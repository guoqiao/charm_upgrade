# charm upgrade

charm upgrade helper.

## Usage

    ./charm_upgrade.py
    ./charm_upgrade.py -s /path/to/juju/status/json

The script relies on output from `juju status --format json`.
If no file specified, it will run the cmd to get output.

## Update json files

There are 3 json files which provide mappings among branchs, commits and revisions for openstack charms.
Here is how to update them:

    ./charm_upgrade.py -b/--update-branch-commit    # update branch_commit.json via github api
    ./charm_upgrade.py -r/--update-revision-commit  # update revision_commit.json via charmstore api
    ./charm_upgrade.py -B/--update-branch-revision  # update branch_revision.json via above 2 files
    ./charm_upgrade.py -a/--update-all              # update all above in order

You only need to run these when charms have updates in git repo or charmstore.
The updated json files should be commited to git.

## GitHub API rate limiting and authentication

We use GitHub API to update `branch_commit.json`, which has [rate limiting](https://developer.github.com/v3/#rate-limiting):

1) for annoymous user: 60 requests/hour (almost useless)
2) for authenticated user: 5000 requests/hour (we need this)

To get authenticated, you can set envvars in either way:

1) OAuth via [Personal access token](https://github.com/settings/tokens) (recommended)

```
GITHUB_TOKEN=<YOUR-TOKEN>
```


2) Basic Auth

```
GITHUB_USER=<YOUR-USERNAME>
GITHUB_PASS=<YOUR-PASSWORD>
```


## Problem

openstack charm git repo has release branches, but charmstore only has linear/incremental revision numbers.
This will cause problem when a branch updated for backport patches.
Let's say we have following git branchs and charmstore revisions for a charm:

- 20.02 -> commit1 -> 1
- 20.05 -> commit2 -> 2

Later we found a bug which needs to backport to both branches.
For 20.05, if it's latest branch, we are ok to release a new revision 3 to charmstore.
However, for 20.02, we can not release, since it will end up with a bigger revision 4 for an old branch.

- 20.02 -> commit1 -> 1
- 20.05 -> commit2 -> 2
- 20.05 -> commit3 -> 3
- 20.02 -> commit4 -> 4 (X)

Because of this defect in charmstore, some charm repo branches are updated (to a new commit), but not released to charmstore.
We can not find branch to commit mappings for theses branches.
One of the solution could be: for each branch, find a list of most recent commits, instead of the latest commit.
But that will introduce much more github api query, or we have to clone each repo to disk.
