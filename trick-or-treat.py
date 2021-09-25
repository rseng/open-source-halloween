#!/usr/bin/env python3

import logging
import tempfile
import fnmatch
import os
from copy import deepcopy
from pathlib import Path
import calendar
import time
import shutil
import hashlib
import yaml
import json
from datetime import datetime

from github import Github
from github.GithubException import UnknownObjectException, RateLimitExceededException
import git
from jinja2 import Environment, FileSystemLoader, select_autoescape

logging.basicConfig(level=logging.INFO)

# We want the root
here = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

env = Environment(
    autoescape=select_autoescape(["html"]), loader=FileSystemLoader("templates")
)

# do not clone LFS files
os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"
g = Github(os.environ["GITHUB_TOKEN"])
repos = []

core_rate_limit = g.get_rate_limit().core


def read_file(filename):
    with open(filename, "r") as fd:
        content = fd.read()
    return content


def read_yaml(filename):
    with open(filename, "r") as fd:
        content = yaml.load(fd, Loader=yaml.FullLoader)
    return content


# Skip parsing these repos!
skips = ["rseng/open-source-halloween"]

# If we already have an existing file, load it.
repos_file = os.path.join(here, "assets", "js", "repos.js")
repos = []
if os.path.exists(repos_file):
    content = read_file(repos_file)
    repos = json.loads("\n".join(content.split("\n")[1:]))


class Repo:
    data_format = 2

    def __init__(self, github_repo, filenames):
        for attr in [
            "full_name",
            "html_url",
            "description",
            "stargazers_count",
            "homepage",
            "size",
            "language",
            "subscribers_count",
            "open_issues",
        ]:
            setattr(self, attr, getattr(github_repo, attr))

        self.avatar = github_repo.__dict__['_rawData']['owner']['avatar_url']
        self.default_branch = github_repo.default_branch
        self.updated_at = github_repo.updated_at.timestamp()
        self.owner = github_repo.owner.login
        self.topics = github_repo.get_topics()
        self.filenames = list(filenames)

        try:
            self.latest_release = github_repo.get_latest_release().tag_name
        except UnknownObjectException:
            # no release
            self.latest_release = None

        self.data_format = Repo.data_format


def rate_limit_wait():
    curr_timestamp = calendar.timegm(time.gmtime())
    reset_timestamp = calendar.timegm(core_rate_limit.reset.timetuple())
    # add 5 seconds to be sure the rate limit has been reset
    sleep_time = max(0, reset_timestamp - curr_timestamp) + 5
    logging.warning(f"Rate limit exceeded, waiting {sleep_time} seconds")
    time.sleep(sleep_time)


def call_rate_limit_aware(func):
    while True:
        try:
            return func()
        except RateLimitExceededException:
            rate_limit_wait()


def call_rate_limit_aware_decorator(func):
    def inner(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except RateLimitExceededException:
                rate_limit_wait()

    return inner


def store_data():

    repos.sort(key=lambda repo: repo["stargazers_count"])

    # Create a copy to work with
    copied = deepcopy(repos)

    # Reverse so the newer are at the front
    copied.reverse()

    # Go through and get newest entries (added first)
    results = {}
    for result in copied:
        if result["full_name"] not in results:
            results[result["full_name"]] = result

    # Save to yaml in data folder
    datapath = os.path.join(here, "_data", "repos.yml")
    results = {}
    for repo in repos:
        results[repo["full_name"]] = repo
    with open(datapath, "w") as out:
        yaml.dump(results, out)

    # This should be unique, only newer repos found
    results = list(results.values())
    print("Saving total of %s results." % len(results))
    results.sort(key=lambda repo: repo["stargazers_count"])

    # Save the js data ready to go, and data for jekyll
    datapath = os.path.join(here, "assets", "js", "repos.js")
    with open(datapath, "w") as out:
        print(env.get_template("repos.js").render(data=results), file=out)


@call_rate_limit_aware_decorator
def combine_results(code_search):
    """
    Given a code search result, organize by repos
    """
    byrepo = {}
    lookup = {}

    for i, filename in enumerate(code_search):

        # attempt to not trigger abuse mechanism
        time.sleep(0.5)
        repo = filename.repository

        if repo.full_name not in byrepo:
            byrepo[repo.full_name] = set()
            lookup[repo.full_name] = repo
        byrepo[repo.full_name].add(filename.path)
    return byrepo, lookup


def validate_spackyaml(filename):
    """
    Ensure that a spack.yaml file has a spack or env directive
    """
    try:
        with open(filename, "r") as fd:
            data = yaml.load(fd, Loader=yaml.FullLoader)
            if "env" not in data and "spack" not in data:
                return False
            return True
    except yaml.YAMLError:
        return False


def get_current_digests(datadir):
    """
    Get current digests so we don't add any repeated files!
    """
    digests = set()
    for root, _, filenames in os.walk(datadir):
        for filename in fnmatch.filter(filenames, "open-source-halloween*"):
            digests.add(get_digest(os.path.join(root, filename)))
    return digests


def get_digest(filename):
    """
    Don't add repeated images of candy (from forks, etc.)
    """
    hasher = hashlib.md5()
    with open(filename, "rb") as fd:
        content = fd.read()
    hasher.update(content)
    return hasher.hexdigest()


def main():
    """
    Entrypoint to trick or treat!
    """
    # Don't parse the vsoch/spack-changes repository!
    code_search = g.search_code(
        "filename:open-source-halloween-%s.png" % datetime.now().year, sort="indexed"
    )

    # Create a directory structure with images
    data_dir = os.path.join(here, "_candy")

    # Keep a record of file hashes so we don't have repeats
    digests = get_current_digests(data_dir)

    # Consolidate filenames by repository
    byrepo, lookup = combine_results(code_search)
    total_count = len(byrepo)

    for i, reponame in enumerate(byrepo):

        # Skip these repos
        if reponame in skips:
            print("Skipping %s" % reponame)
            continue

        # List of files
        files = byrepo[reponame]
        repo = lookup[reponame]
        if i % 10 == 0:
            logging.info(f"{i} of {total_count} results done")

        repo_dir = os.path.join(data_dir, repo.full_name)
        logging.info(f"Processing {repo.full_name}.")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)

            # Create the repo directory
            if not os.path.exists(repo_dir):
                os.makedirs(repo_dir)

            # clone main branch
            try:
                git.Repo.clone_from(repo.clone_url, str(tmp), depth=1)
            except git.GitCommandError:
                continue

            # We will update files if a file doesn't exist or is invalid
            updated_files = []

            # For each spack yaml, validate
            for filename in files:
                candyfile = tmp / filename
                if not candyfile.exists():
                    continue

                # Calculate a hash so we don't include repeated files
                digest = get_digest(candyfile)
                if digest in digests:
                    print("Already seen %s, skipping" % filename)
                    continue
                digests.add(digest)

                savepath = os.path.join(repo_dir, filename)
                savedir = os.path.dirname(savepath)
                if not os.path.exists(savedir):
                    os.makedirs(savedir)

                shutil.copyfile(str(candyfile), savepath)
                updated_files.append(filename)

        # Don't add repos without any spack.yaml files
        if not updated_files:
            continue

        call_rate_limit_aware(lambda: repos.append(Repo(repo, updated_files).__dict__))
        #        repos.append(Repo(repo, updated_files).__dict__)

        if len(repos) % 20 == 0:
            logging.info("Storing intermediate results.")
            store_data()

    # one final save
    store_data()


if __name__ == "__main__":
    main()
