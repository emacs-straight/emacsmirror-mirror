#!/usr/bin/env python3

import datetime
import github
import os
import pathlib
import shutil
import subprocess
import sys

os.chdir(os.path.dirname(__file__))

def remove_prefix(prefix, string):
    if string.startswith(prefix):
        return string[len(prefix):]
    else:
        return string

def log(message):
    print(message, file=sys.stderr)

def die(message):
    log("gnu_elpa_mirror: " + message)
    sys.exit(1)

try:
    ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
except KeyError:
    die("please export ACCESS_TOKEN to a valid GitHub API token")

def clone_git_repo(git_url, repo_dir, shallow, all_branches, private_url):
    if not repo_dir.is_dir():
        cmd = ["git", "clone"]
        if shallow:
            cmd.extend(["--depth", "1"])
            if all_branches:
                cmd.append("--no-single-branch")
        cmd.extend([git_url, repo_dir])
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError:
            if private_url:
                die("cloning repository failed (details omitted for security)")
            raise
    else:
        result = subprocess.run(
            ["git", "symbolic-ref", "HEAD"],
            cwd=repo_dir, check=True, stdout=subprocess.PIPE)
        branch = remove_prefix("refs/heads/", result.stdout.decode().strip())
        ref = "refs/remotes/origin/{}".format(branch)
        subprocess.run(["git", "fetch"], cwd=repo_dir, check=True)
        result = subprocess.run(["git", "show-ref", ref], cwd=repo_dir,
                                stdout=subprocess.DEVNULL)
        # Check if there is a master branch to merge from upstream.
        # Also, avoid creating merges or rebases due to a diverging
        # history.
        if result.returncode == 0:
            subprocess.run(["git", "reset", "--hard", ref],
                           cwd=repo_dir, check=True)

def delete_contents(path):
    for entry in sorted(path.iterdir()):
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            try:
                entry.unlink()
            except FileNotFoundError:
                pass

def stage_and_commit(repo_dir, message, data):
    # Note the use of --force because some packages like AUCTeX need
    # files to be checked into version control that are nevertheless
    # in their .gitignore. See [1].
    #
    # [1]: https://github.com/raxod502/straight.el/issues/299
    subprocess.run(
        ["git", "add", "--all", "--force"], cwd=repo_dir, check=True)
    anything_staged = (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir).returncode != 0)
    if anything_staged:
        subprocess.run(
            ["git",
             "-c", "user.name=GNU ELPA Mirror Bot",
             "-c", "user.email=emacs-devel@gnu.org",
             "commit", "-m",
             ("{}\n\n"
              "Timestamp: {}\n"
              "GNU ELPA commit: {}\n"
              "Emacs commit: {}")
             .format(message,
                     data["timestamp"],
                     data["gnu_elpa_commit"],
                     data["emacs_commit"])],
            cwd=repo_dir, check=True)
    else:
        log("(no changes)")

# https://savannah.gnu.org/git/?group=emacs
GNU_ELPA_GIT_URL = "https://git.savannah.gnu.org/git/emacs/elpa.git"
EMACS_GIT_URL = "https://git.savannah.gnu.org/git/emacs.git"

GNU_ELPA_SUBDIR = pathlib.Path("gnu-elpa")
GNU_ELPA_PACKAGES_SUBDIR = GNU_ELPA_SUBDIR / "packages"
EMACS_SUBDIR = GNU_ELPA_SUBDIR / "emacs"
REPOS_SUBDIR = pathlib.Path("repos")

def mirror(args):
    api = github.Github(ACCESS_TOKEN)
    log("--> clone/update GNU ELPA")
    clone_git_repo(
        GNU_ELPA_GIT_URL, GNU_ELPA_SUBDIR,
        shallow=False, all_branches=True, private_url=False)
    log("--> clone/update Emacs")
    clone_git_repo(
        EMACS_GIT_URL, EMACS_SUBDIR,
        shallow=False, all_branches=False, private_url=False)
    log("--> check timestamp and commit hashes")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gnu_elpa_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=GNU_ELPA_SUBDIR, stdout=subprocess.PIPE,
        check=True).stdout.decode().strip()
    emacs_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=EMACS_SUBDIR, stdout=subprocess.PIPE,
        check=True).stdout.decode().strip()
    commit_data = {
        "timestamp": timestamp,
        "gnu_elpa_commit": gnu_elpa_commit,
        "emacs_commit": emacs_commit,
    }
    log("--> install bugfix in GNU ELPA build script")
    subprocess.run(
        ["git", "checkout", "admin/archive-contents.el"], cwd=GNU_ELPA_SUBDIR)
    with open(GNU_ELPA_SUBDIR / "admin" / "archive-contents.el", "r+") as f:
        contents = f.read()
        contents = contents.replace(
            '(cons file-pattern "")',
            '(cons file-pattern (file-name-nondirectory file-pattern))')
        f.seek(0)
        f.truncate()
        f.write(contents)
    log("--> retrieve/update GNU ELPA external packages")
    subprocess.run(["make", "externals"], cwd=GNU_ELPA_SUBDIR, check=True)
    log("--> get list of mirror repositories")
    existing_repos = []
    for repo in api.get_user("emacs-straight").get_repos():
        existing_repos.append(repo.name)
    packages = []
    for subdir in sorted(GNU_ELPA_PACKAGES_SUBDIR.iterdir()):
        if not subdir.is_dir():
            continue
        # Prevent monkey business.
        if subdir.name == "gnu-elpa-mirror":
            continue
        packages.append(subdir.name)
    log("--> clone/update mirror repositories")
    org = api.get_organization("emacs-straight")
    REPOS_SUBDIR.mkdir(exist_ok=True)
    for package in packages:
        git_url = ("https://raxod502:{}@github.com/emacs-straight/{}.git"
                   .format(ACCESS_TOKEN, package))
        repo_dir = REPOS_SUBDIR / package
        if package not in existing_repos:
            log("----> create mirror repository {}".format(package))
            org.create_repo(
                package,
                description=("Mirror of the {} package from GNU ELPA"
                             .format(package)),
                homepage=("https://elpa.gnu.org/packages/{}.html"
                          .format(package)),
                has_issues=False,
                has_wiki=False,
                has_projects=False,
                auto_init=False)
        if "--skip-mirror-pulls" in args and repo_dir.is_dir():
            continue
        log("----> clone/update mirror repository {}".format(package))
        clone_git_repo(git_url, repo_dir,
                       shallow=True, all_branches=False, private_url=True)
    log("--> update mirrored packages")
    for package in packages:
        log("----> update package {}".format(package))
        package_dir = GNU_ELPA_PACKAGES_SUBDIR / package
        repo_dir = REPOS_SUBDIR / package
        delete_contents(repo_dir)
        for source in sorted(package_dir.iterdir()):
            if source.name == ".git":
                continue
            target = repo_dir / source.name
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, target)
            else:
                shutil.copyfile(source, target)
        stage_and_commit(repo_dir, "Update " + package, commit_data)
    if "--skip-mirror-pushes" not in args:
        log("--> push changes to mirrored packages")
        for package in packages:
            log("----> push changes to package {}".format(package))
            repo_dir = REPOS_SUBDIR / package
            subprocess.run(["git", "push", "origin", "master"],
                           cwd=repo_dir, check=True)
    git_url = ("https://raxod502:{}@github.com/emacs-straight/{}.git"
               .format(ACCESS_TOKEN, "gnu-elpa-mirror"))
    repo_dir = REPOS_SUBDIR / "gnu-elpa-mirror"
    if "gnu-elpa-mirror" not in existing_repos:
        log("--> create mirror list repository")
        org.create_repo(
            "gnu-elpa-mirror",
            description="List packages mirrored from GNU ELPA",
            homepage="https://elpa.gnu.org/packages/",
            has_issues=False,
            has_wiki=False,
            has_projects=False,
            auto_init=False)
    log("--> clone/update mirror list repository")
    clone_git_repo(git_url, repo_dir,
                   shallow=True, all_branches=False, private_url=True)
    log("--> update mirror list repository")
    delete_contents(repo_dir)
    for package in packages:
        with open(repo_dir / package, "w"):
            pass
    stage_and_commit(repo_dir, "Update mirror list", commit_data)
    log("--> push changes to mirror list repository")
    subprocess.run(["git", "push", "origin", "master"],
                   cwd=repo_dir, check=True)

if __name__ == "__main__":
    mirror(sys.argv[1:])
