"""Refresh the neofetch-style numbers in dark_mode.svg / light_mode.svg.

Run by .github/workflows/build.yaml on a schedule, or by hand:
    ACCESS_TOKEN=<token> USER_NAME=FATFATHAO python today.py

Everything in the SVGs except the fields listed in WIDTHS is static.
"""
import datetime
import hashlib
import json
import os
import time
from pathlib import Path

import requests
from lxml import etree

GRAPHQL = 'https://api.github.com/graphql'
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
BIRTHDAY = datetime.date(2005, 2, 10)  # drives the "Uptime" line
SVGS = ('dark_mode.svg', 'light_mode.svg')
# Field widths must match the generator that produced the SVGs, so dots + value == width.
WIDTHS = {'age_data': 22, 'repo_data': 5, 'star_data': 12, 'commit_data': 18,
          'follower_data': 10, 'pr_data': 18, 'issue_data': 8, 'loc_data': 9}


def graphql(query, variables):
    """POST to GraphQL, retrying the 5xx responses GitHub returns under sustained load."""
    for attempt in range(4):
        r = requests.post(GRAPHQL, json={'query': query, 'variables': variables}, headers=HEADERS)
        if r.status_code < 500:
            break
        time.sleep(2 ** attempt)
    r.raise_for_status()
    if 'errors' in r.json():
        raise SystemExit(f'GraphQL error: {r.json()["errors"]}')
    return r.json()['data']


def search_count(q):
    """totalCount of a search query; GitHub's search API needs its own query shape."""
    r = requests.post(GRAPHQL, headers=HEADERS,
                      json={'query': 'query($q:String!){search(query:$q,type:ISSUE,first:1){issueCount}}',
                            'variables': {'q': q}})
    r.raise_for_status()
    return r.json()['data']['search']['issueCount']


def uptime():
    """Whole months plus leftover days since BIRTHDAY, as 'N years, N months, N days'."""
    today = datetime.date.today()
    months = (today.year - BIRTHDAY.year) * 12 + today.month - BIRTHDAY.month
    if today.day >= BIRTHDAY.day:
        days = today.day - BIRTHDAY.day
    else:
        months -= 1
        prev_month_end = today.replace(day=1) - datetime.timedelta(days=1)
        days = prev_month_end.day - BIRTHDAY.day + today.day
    return f'{months // 12} years, {months % 12} months, {days} days'


REPO_QUERY = '''query($c:String){viewer{
  repositories(first:100,after:$c,ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER]){
    pageInfo{hasNextPage endCursor}
    edges{node{nameWithOwner defaultBranchRef{target{... on Commit{oid}}}}}}}}'''

HISTORY_QUERY = '''query($o:String!,$n:String!,$c:String,$a:ID){repository(owner:$o,name:$n){
  defaultBranchRef{target{... on Commit{history(first:100,after:$c,author:{id:$a}){
    pageInfo{endCursor hasNextPage}
    edges{node{additions deletions}}}}}}}}'''


def repos_i_committed_to():
    """nameWithOwner -> branch head oid, for every repo I can be attributed commits in.

    Uses every affiliation rather than contributionsCollection, because that only reports
    commits to non-owned repos and would silently drop my own (dotfiles, Taikou5-Refactor).
    """
    repos = {}
    cursor = None
    while True:
        page = graphql(REPO_QUERY, {'c': cursor})['viewer']['repositories']
        for edge in page['edges']:
            repo = edge['node']
            branch = repo['defaultBranchRef']
            repos[repo['nameWithOwner']] = branch['target']['oid'] if branch else None
        if not page['pageInfo']['hasNextPage']:
            return repos
        cursor = page['pageInfo']['endCursor']


def loc_totals(repos, owner_id):
    """Sum my added/deleted lines over each repo's default-branch history.

    GraphQL is used instead of /stats/contributors because that endpoint only ever
    resolves for repositories you own or push to; everywhere else it returns 202 forever.
    Results are cached against the branch head, so a run only re-walks repos that moved.
    """
    cache_file = Path('cache') / f'{hashlib.sha256(USER_NAME.encode()).hexdigest()}.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}

    def flush():
        cache_file.parent.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=1, sort_keys=True))

    added = deleted = 0
    for full_name, oid in sorted(repos.items()):
        owner, name = full_name.split('/', 1)
        hit = cache.get(full_name)
        if hit and oid and hit[0] == oid:
            added, deleted = added + hit[1], deleted + hit[2]
            continue
        add = delete = 0
        cursor = None
        while True:
            # author filter is server-side, so upstream commits are never fetched
            data = graphql(HISTORY_QUERY, {'o': owner, 'n': name, 'c': cursor, 'a': owner_id})
            branch = data['repository']['defaultBranchRef']
            if not branch:  # empty repo
                break
            history = branch['target']['history']
            for edge in history['edges']:
                add += edge['node']['additions']
                delete += edge['node']['deletions']
            if not history['pageInfo']['hasNextPage']:
                break
            cursor = history['pageInfo']['endCursor']
        cache[full_name] = [oid, add, delete]
        flush()  # save as we go, so a mid-run failure resumes instead of re-walking
        added, deleted = added + add, deleted + delete
    return added, deleted


def svg_overwrite(filename, values):
    tree = etree.parse(filename)
    root = tree.getroot()
    for element_id, text in values.items():
        node = root.find(f".//*[@id='{element_id}']")
        if node is None:
            raise SystemExit(f'{filename}: no element with id={element_id!r}')
        node.text = text
        dots = root.find(f".//*[@id='{element_id}_dots']")
        if dots is not None and element_id in WIDTHS:
            pad = max(0, WIDTHS[element_id] - len(text))
            dots.text = '' if pad == 0 else ' ' if pad == 1 else '. ' if pad == 2 else \
                ' ' + '.' * (pad - 2) + ' '
    tree.write(filename, encoding='utf-8', xml_declaration=True)


if __name__ == '__main__':
    stats = graphql(
        '''query($l:String!){user(login:$l){
             id
             followers{totalCount}
             repositories(first:100,ownerAffiliations:OWNER,isFork:false){
               totalCount edges{node{stargazerCount}}}
             repositoriesContributedTo(first:100){totalCount}
             contributionsCollection{totalCommitContributions}}}''',
        {'l': USER_NAME})['user']
    added, deleted = loc_totals(repos_i_committed_to(), stats['id'])

    values = {
        'age_data': uptime(),
        'repo_data': f"{stats['repositories']['totalCount']:,}",
        'contrib_data': str(stats['repositoriesContributedTo']['totalCount']),
        'star_data': f"{sum(e['node']['stargazerCount'] for e in stats['repositories']['edges']):,}",
        'commit_data': f"{stats['contributionsCollection']['totalCommitContributions']:,}",
        'follower_data': f"{stats['followers']['totalCount']:,}",
        'pr_data': f"{search_count(f'author:{USER_NAME} type:pr'):,}",
        'issue_data': f"{search_count(f'author:{USER_NAME} type:issue'):,}",
        'loc_data': f'{added - deleted:,}',
        'loc_add': f'{added:,}',
        'loc_del': f'{deleted:,}',
    }
    for svg in SVGS:
        svg_overwrite(svg, values)
        print('updated', svg)
