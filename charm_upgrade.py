#!/usr/bin/python3
import sys
import time
import json
import argparse
import subprocess
import logging
from os import getenv
from os.path import abspath, dirname, join
from collections import defaultdict

import requests

LOG = logging.getLogger(__name__)
LOG_FMT = '%(asctime)s %(levelname)s: %(message)s'

# charms upgrade order
# https://docs.openstack.org/project-deploy-guide/charm-deployment-guide/latest/app-upgrade-openstack.html#upgrade-order
# https://wiki.canonical.com/CDO/IS/Bootstack/Playbooks/OpenstackCharmUpgrades#Upgrade_Order

NA = '--'
ORDER_MAX = 999
# only care about these branches
BRANCHES = ['19.04', '19.07', '19.10', '20.02', '20.05']

HERE = abspath(dirname(__file__))
# branch: github release branch short name, e.g.: 20.05
# commit: git hash, e.g.: 892016dff67830ac16f46e17aefecb3d231063ae
# revision: charm store rev 303 as str
FILE_BRANCH_COMMIT = join(HERE, 'branch_commit.json')
FILE_REVISION_COMMIT = join(HERE, 'revision_commit.json')
FILE_BRANCH_REVISION = join(HERE, 'branch_revision.json')

# will upgrade in this order
OPENSTACK_CHAMRS = [
    "barbican-vault", "barbican",
    "keystone-ldap", "keystone",
    "rabbitmq-server",
    "nova-cloud-controller",
    "nova-compute",
    "neutron-openvswitch",
    "neutron-api",
    "neutron-gateway",
    "octavia-dashboard", "octavia",
    "cinder-backup", "cinder-ceph", "cinder",
    "glance",
    "heat",
    "aodh",
    "swift-proxy", "swift-storage",
    "ceph-mon", "ceph-radosgw", "ceph-osd",
    "ceilometer-agent", "ceilometer",
    "gnocchi",
    "designate-bind", "designate",
    "openstack-dashboard",
    "hacluster",
    "vault",
]

# lma charms will be ugpraded after openstack charms, in this order
LMA_CHARMS = [
    "canonical-livepatch",
    "thruk-agent", "nrpe", "nagios",
    "grafana",
    "prometheus-ceph-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-openstack-exporter",
    "prometheus",
    "prometheus2",
    "telegraf",
    "kibana", "filebeat", "graylog", "elasticsearch",
    "etcd",
    "easyrsa",
    "mysql", "percona-cluster",
    "mongodb",
    "ntp",
]


ORDERS = {charm: i for i, charm in enumerate(OPENSTACK_CHAMRS + LMA_CHARMS)}


def pretty_json(obj):
    return json.dumps(obj, indent=4)


def print_json(obj):
    print(pretty_json(obj))


def load_json(path):
    LOG.debug('load json from %s', path)
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, mode='w') as f:
        f.write(pretty_json(data))
    LOG.info('save json to %s', path)


def get_cmd_output(cmd, is_json=False):
    # cmd is a list
    LOG.info('run cmd: %s', ' '.join(cmd))
    output = subprocess.check_output(cmd).decode('utf8')
    if is_json:
        output = json.loads(output)
        LOG.debug('cmd output as json: %s', pretty_json(output))
    else:
        LOG.debug('cmd output: %s', output)
    return output


def get_url_output(url, is_json=False, **kwargs):
    resp = requests.get(url, **kwargs)
    if is_json:
        output = resp.json()
        LOG.debug('url output as json: %s', pretty_json(output))
    else:
        output = resp.text
        LOG.debug('url output: %s', output)
    return output


def get_repo_branch_commit_map(repo, owner='openstack', branch_prefix='stable'):
    """GitHub repo branch to commit map.


    Returns a dict like:
    {
        '20.05': 'd4be28550008426d5d8ac6af2ea93ce0da685390',
        '20.02': '2ec5fe7a873b2b5836f15dfab4ad569c9b7ab0f7',
        ...
    }

    e.g.: for repo charm-keystone:
    curl https://api.github.com/repos/openstack/charm-keystone/git/matching-refs/heads/stable
    [
        ...
        {
            "ref": "refs/heads/stable/20.05",
            "node_id": "MDM6UmVmNTI4NTg3Njc6cmVmcy9oZWFkcy9zdGFibGUvMjAuMDU=",
            "url": "https://api.github.com/repos/openstack/charm-keystone/git/refs/heads/stable/20.05",
            "object": {
                "sha": "d4be28550008426d5d8ac6af2ea93ce0da685390",
                "type": "commit",
                "url": "https://api.github.com/repos/openstack/charm-keystone/git/commits/d4be28550008426d5d8ac6af2ea93ce0da685390"
            }
        }
    ]

    ref: https://developer.github.com/v3/git/refs/#list-matching-references
    """
    url = 'https://api.github.com/repos/{owner}/{repo}/git/matching-refs/heads/{branch_prefix}'.format(
        owner=owner, repo=repo, branch_prefix=branch_prefix)
    LOG.debug(url)

    github_token = getenv('GITHUB_TOKEN')
    github_user = getenv('GITHUB_USER')
    github_pass = getenv('GITHUB_PASS')

    if github_token:
        # https://developer.github.com/v3/#oauth2-token-sent-in-a-header
        # curl -H "Authorization: token OAUTH-TOKEN" https://api.github.com
        LOG.debug('OAuth2 token used for github api, 5000 requests/hour')
        resp = requests.get(url, headers={'Authorization': 'token {}'.format(github_token)})
    elif github_user and github_pass:
        LOG.debug('basic auth used for github api, 5000 requests/hour')
        resp = requests.get(url, auth=(github_user, github_pass))
    else:
        LOG.warning('no auth used for github api, 60 requests/hour, sleeping 3 secs...')
        time.sleep(3)  # slow down for github api limit
        resp = requests.get(url)

    items = resp.json()
    # {'20.05': 'd4be28...', '20.02': 'd3812d...', ...}
    return {branch['ref'].rsplit('/')[-1]: branch['object']['sha'] for branch in items}


def get_revision_commit(charm, rev=None):
    """Get commit for charm revision.


    Args:
        rev: revision number in str, since json only allow str keys.
        If None, will return commit for latest revision.
        Mutliple revs may have same commit.

    Returns: tuple (rev, commit)

    Get latest revision:
    curl https://api.jujucharms.com/charmstore/v5/cinder/meta/any?include=extra-info'

    Get specified revision:
    curl https://api.jujucharms.com/charmstore/v5/cinder-303/meta/any?include=extra-info'

    Output:

    {
        Id: "cs:cinder-303",
        Meta: {
            extra-info: {
                vcs-revisions: [{
                    authors: [
                        {
                            name: "David Ames",
                            email: "david.ames@canonical.com"
                        }
                    ],
                    date: "2020-05-21T09:54:19-07:00",
                    message: "Updates for stable branch...",
                    commit: "9b8a2305a00a22903e0cc210a57fc1e27333859e"
                }]
            }
        }
    }

    But sometimes it returns this:

    {
        "Id": "cs:barbican-vault-15",
        "Meta": {
            "extra-info": {}
        }
    }

    When this happens, we fallback to get repo info from this url:

    https://api.jujucharms.com/charmstore/v5/barbican-vault-4/archive/repo-info

    commit-sha-1: a52f533b54abce67fb3df642cda5695568fbfb90
    commit-short: a52f533
    branch: HEAD
    remote: https://github.com/openstack/charm-barbican-vault
    info-generated: Fri May 31 06:47:59 UTC 2019
    note: This file should exist only in a built or released charm artifact (not in the charm source code tree).

    """
    if rev:  # rev is str
        name = '{}-{}'.format(charm, rev)
    else:
        name = charm

    url = 'https://api.jujucharms.com/charmstore/v5/{}/meta/any?include=extra-info'.format(name)
    data = get_url_output(url, is_json=True)

    rev = ''
    commit = ''

    Id = data.get('Id')
    if Id and '-' in Id:
        rev = Id.rsplit('-')[-1]
        vcs_revisions = data.get('Meta', {}).get('extra-info', {}).get('vcs-revisions', {})
        if vcs_revisions:
            commit = vcs_revisions[0].get('commit')

        if not commit:
            # if Id exists, but no commit, fall back to use repo-info file content
            url = 'https://api.jujucharms.com/charmstore/v5/{}-{}/archive/repo-info'.format(charm, rev)
            text = get_url_output(url)
            for line in text.strip().splitlines():
                line = line.strip()
                if ':' in line:
                    key, value = line.split(':', maxsplit=1)
                    if key == 'commit-sha-1':
                        commit = value.strip()
                        LOG.debug('%s %s %s', charm, rev, line)
                        break

    return rev, commit


def update_charm_revisions(charm, revisions):
    """Update rev -> commit mapping for a charm.

    Args:
        revisions (dict): existing rev -> commit mapping

    Returns:
        revisions (dict): will be updated in place
        changed (int): how many commits have changed
    """
    changed = 0
    rev, commit = get_revision_commit(charm)  # get current/latest rev commit
    if rev not in revisions:
        revisions[rev] = commit
        changed += 1
    n_rev = int(rev)
    missing_revs = 0
    while n_rev > 0:
        n_rev -= 1
        rev = str(n_rev)
        if rev in revisions:
            LOG.info('%s %s exists, skip', charm, rev)
            continue
        rev, commit = get_revision_commit(charm, rev=rev)
        if rev and commit:
            missing_revs = 0
            revisions[rev] = commit
            changed += 1
            LOG.info('%s %s: %s', charm, rev, commit)
            time.sleep(1)  # slow down to avoid api rate limit
        else:
            LOG.warning('no commit for charm %s rev', charm)
            missing_revs += 1
            if missing_revs >= 3:
                LOG.warning('more than 3 revs missing for %s, break', charm)
                break
    return changed


def update_branch_commit():
    data = {charm: get_repo_branch_commit_map('charm-' + charm) for charm in OPENSTACK_CHAMRS}
    save_json(FILE_BRANCH_COMMIT, data)


def update_revision_commit():
    # update charms revisions based on existing data
    for charm in OPENSTACK_CHAMRS:
        current_revisions = load_json(FILE_REVISION_COMMIT)
        if charm not in current_revisions:  # new added charm
            current_revisions[charm] = {}
        # will update `current_revisions` in place, return changed count
        changed = update_charm_revisions(charm, current_revisions[charm])
        if changed:
            save_json(FILE_REVISION_COMMIT, current_revisions)


def update_branch_revision():
    # convert rev -> commit mapping to commit -> max_rev
    OPENSTACK_CHARMS_COMMIT_REVISION = {}
    for charm, revisions in load_json(FILE_REVISION_COMMIT).items():
        commits = {}
        for rev, commit in revisions.items():
            # it's possible N revs have same commit, we take the largest rev
            if int(rev) > int(commits.get(commit, 0)):
                commits[commit] = rev
        OPENSTACK_CHARMS_COMMIT_REVISION[charm] = commits

    # 20.05 -> 5dcbfd..
    OPENSTACK_CHARMS_BRANCH_COMMIT = load_json(FILE_BRANCH_COMMIT)
    # 20.05 -> 303
    data = defaultdict(dict)
    for charm, dict_branch_commit in OPENSTACK_CHARMS_BRANCH_COMMIT.items():
        dict_branch_revision = {}
        for branch, commit in dict_branch_commit.items():
            rev = OPENSTACK_CHARMS_COMMIT_REVISION[charm].get(commit, '')
            if rev:
                dict_branch_revision[branch] = rev
        data[charm] = dict_branch_revision

    save_json(FILE_BRANCH_REVISION, data)


def yesno(boolean, yes, no):
    """ternary in python: boolean? yes:no"""
    return (no, yes)[boolean]


def mark_revs(revs, current_rev=''):
    """add * before current rev"""
    if current_rev:
        return ['{}{}'.format(yesno(str(current_rev) == str(rev), '*', ''), rev) for rev in revs]
    else:
        return revs


def print_app(order, app, current, latest, revs, units):
    revs = ['{:>7}'.format(rev) for rev in revs]
    print('{:>2}  {:<30} {:<40} {:>7} {} {:>5}'.format(order, app, current, latest, ''.join(revs), units))


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='charm upgrade helper'
    )

    parser.add_argument(
        '-b', '--update-branch-commit', dest='update_branch_commit', action='store_true',
        help='Update charm branch commit mapping with github api, save to file')

    parser.add_argument(
        '-r', '--update-revision-commit', dest='update_revision_commit', action='store_true',
        help='Update charm revision commit mapping with charmstore api, save to file')

    parser.add_argument(
        '-B', '--update-branch-revision', dest='update_branch_revision', action='store_true',
        help='Update charm branch revision mapping based on existing data, save to file')

    parser.add_argument(
        '-a', '--update-all', dest='update_all', action='store_true',
        help='Update all files')

    parser.add_argument(
        '-s', '--status-json-file',
        dest='status_json_file',
        help='Load juju status json data from this file')

    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Be verbose')

    cli = parser.parse_args()
    logging.basicConfig(level=['INFO', 'DEBUG'][cli.verbose], format=LOG_FMT)

    if cli.update_branch_commit:
        update_branch_commit()
        sys.exit()
    elif cli.update_revision_commit:
        update_revision_commit()
        sys.exit()
    elif cli.update_branch_revision:
        update_branch_revision()
        sys.exit()
    elif cli.update_all:
        update_branch_commit()
        update_revision_commit()
        update_branch_revision()
        sys.exit()

    if cli.status_json_file:
        # if user specified a file, read from there, helpful for local debug
        juju_status = load_json(cli.status_json_file)
    else:
        # if default file not exist, generate/save/cache it for reuse
        juju_status = get_cmd_output(['juju', 'status', '--format', 'json'], is_json=True)

    # merge all data into a ordered list
    apps = []
    for app_name, app_data in juju_status['applications'].items():
        data = app_data.copy()
        data['name'] = app_name
        charm_name = app_data['charm-name']
        charm_uri = app_data['charm']
        data['charm-uri'] = charm_uri
        data['order'] = ORDERS.get(charm_name, ORDER_MAX)  # make it large to sort at last
        data['charm-release'] = 'NA'
        data['units'] = len(app_data.get('units', {})) or ''
        apps.append(data)

    branch_to_revision = load_json(FILE_BRANCH_REVISION)
    print('[help: N: order, *: current, {}: NA]'.format(NA))
    print_app('N', 'app', 'current', 'latest', BRANCHES, 'units')  # title
    for app in sorted(apps, key=lambda app: app['order']):
        order = app['order']
        if order == ORDER_MAX:
            order = NA
        charm_name = app['charm-name']
        revs = [branch_to_revision.get(charm_name, {}).get(branch, '') for branch in BRANCHES]
        can_upgrade_to = app.get('can-upgrade-to', '')
        if can_upgrade_to and '-' in can_upgrade_to:
            latest_rev = can_upgrade_to.rsplit('-')[-1]
        else:
            latest_rev = NA
        print_app(
            order,
            app['name'],
            app['charm-uri'][:40],
            latest_rev,
            mark_revs(revs, current_rev=app['charm-rev']),
            app['units'],
        )


if __name__ == '__main__':
    main()
