"""
the basic higher-level logic of ugit
to implement higher-level structures for storing directories
"""
import itertools
import operator
import os
import string

from collections import deque, namedtuple

from . import data
from . import diff

def init():
    """
    make HEAD point to a ref refs/heads/master 
    so that the repository has an initial branch, 
    called master (meaning the main branch). 
    The branch could have been named anything we'd like but master is the standard name
    used for the first branch in Git.
    """
    data.init()
    # HEAD is a symbolic ref that points to master
    data.update_ref('HEAD', data.RefValue(symbolic=True, value='refs/heads/master'))

def write_tree():
    """
    scan the directory recursively, writing tree objects for each directory from index. 
    """
    """
    create a tree to represent the directory recursively
    
    iterate through every level of the given directory,
    save separate levels of the directory as different entries
    
    for a level of the directory, iterate every item in this directory,  
    then create a tree for this level and return it
    if the item is a file: get and save oid of this file
    if the item is a directory: use recursion to go to the deeper level, 
                                then get the return: an oid that deeper level's tree
    
    :return: the oid of the tree of the directory
    """
    # Index is flat list, we need it as a tree of dicts
    index_as_tree = {}
    with data.get_index() as index:
        for path, oid in index.items():
            path = path.split('/')
            dirpath, filename = path[:-1], path[-1]
            
            current = index_as_tree
            # Find the dict for the directory of this file
            for dirname in dirpath:
                current = current.setdefault(dirname, {})
            current[filename] = oid
    
    def write_tree_recursive(tree_dict):
        """
        work with the tree of dicts and write them to the objects store
        """
        entries = []
        for name, value in tree_dict().items():
            if type(value) is dict:
                # get an oid of the tree that can represent the directory
                type_ = 'tree'
                oid = write_tree_recursive(value)
            else:
                type_ = 'blob'
                oid = value
                entries.append ((name, oid, type_))
        
        # create a tree for this level of the directory
        tree = ''.join(f'{type_} {oid} {name}\n'
                    for name, oid, type_ in sorted(entries))

        # return the oid of the tree (the content in files in /objects show a extra tree)
        return data.hash_object(tree.encode(), 'tree')
    
    return write_tree_recursive(index_as_tree)

def _iter_tree_entries(oid):
    """
    a generator that will take an OID of a tree, 
    tokenize it line-by-line and yield the raw string values.
    """
    if not oid:
        return
    tree = data.get_object(oid, 'tree')
    for entry in tree.decode().splitlines():
        type_, oid, name = entry.split(' ', 2)
        # yield a generator
        # https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do 
        yield type_, oid, name

def get_tree(oid, base_path=""):
    """
    uses '_iter_tree_entries' to recursively parse a tree into a dictionary
    
    :return: a dictionary {path: oid} contains every node in the tree
    """
    result = {}
    for type_, oid, name in _iter_tree_entries(oid):
        assert '/' not in name
        # . :'the current folder'.
        # .. :'the folder containing the current folder'.
        assert name not in ('..', '.')
        path = base_path + name
        if type_ == 'blob':
            result[path] = oid
        elif type_ == 'tree':
            # 'update()' inserts the specified items to the dictionary.
            result.update(get_tree(oid, f'{path}/'))
        else:
            assert False, f'Unknow tree entry {type_}'
    return result

def get_working_tree():
    """
    walk over all files in the working directory, 
    
    :return: a dict {file path : hash(object in the file)}
    This dictionary will represent a "tree" without actually writing a tree object.
    """
    result = {}
    for root, _, filenames in os.walk('.'):
        for filename in filenames:
            path = os.path.relpath(f'{root}/{filename}')
            if is_ignored(path) or not os.path.isfile(path):
                continue
            with open(path, 'rb') as f:
                result[path] = data.hash_object(f.read())
    return result

def _empty_current_directory():
    """
    delete all existing stuff before reading
    """
    # os.walk()
    # Generate the file names in a directory tree 
    # by walking the tree either top-down or bottom-up. 
    # :root: is a string, the path to the directory
    # :dirnames:  is a list of the names of the subdirectories in dirpath 
    #            (including symlinks to directories, and excluding '.' and '..')
    # :filenames: is a list of the names of the non-directory files in root
    for root, dirnames, filenames in os.walk('.', topdown=False):
        # iterate every files
        for filename in filenames:
            # Return a relative filepath to path 
            path = os.path.relpath(f'{root}/{filename}')
            if is_ignored(path) or not os.path.isfile(path):
                continue
            os.remove(path)
        # iterate every directory
        for dirname in dirnames:
            path = os.path.relpath(f'{root}/{dirname}')
            if is_ignored(path):
                continue
            try:
                # delete a empty directory
                os.rmdir(path)
            except(FileNotFoundError, OSError):
                # Deletion might fail if the directory contains ignored files,
                # so it's OK
                pass

def read_tree(tree_oid, upload_working=False):
    """
    uses 'get_tree' to get {path : oid}
    update index with 'get_tree' result
    when checkout
    """
    with data.get_index() as index:
        index.clear()
        index.update(get_tree(tree_oid))

        if upload_working:
            _checkout_index(index)

def read_tree_merged(t_base, t_HEAD, t_other, update_working=False):
    """
    calls diff.merge_trees()
    writes the resulting merged tree to the working directory
    """
    with data.get_index() as index:
        index.clear()
        index.update(diff.merge_trees(
            get_tree(t_base),
            get_tree(t_HEAD),
            get_tree(t_other)
        ))

        if update_working:
            _checkout_index(index)    

def get_index_tree():
    """
    return index tree
    """
    with data.get_index() as index:
        return index

def _checkout_index(index):
    _empty_current_directory()
    for path, oid in index.items():
        os.makedirs(os.path.dirname(f'./{path}'), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data.get_object(oid, 'blob'))


def commit(message):
    """
    write the current oid, parent oid and the commit message to the commit object
    then write the cureent commit's oid to the HEAD file
    
    This is basically a linked list implemented over the object database.
    
    :return: the oid of commit (current commit and parent commit)
    """
    commit = f'tree {write_tree()}\n'
    
    # get oid from the HEAD file
    HEAD = data.get_ref('HEAD').value
    if HEAD:
        commit += f'parent {HEAD}\n'
    
    # take MERGE_HEAD into account and delete it after commit
    MERGE_HEAD = data.get_ref('MERGE_HEAD').value
    if MERGE_HEAD:
        commit += f'parent {MERGE_HEAD}\n'
        data.delete_ref('MERGE_HEAD', deref=False)
    
    commit += '\n'
    commit += f'{message}\n'
    
    oid = data.hash_object(commit.encode(), 'commit')
    
    data.update_ref('HEAD', data.RefValue(symbolic=False, value=oid))
    
    return oid

def checkout(name):
    """
    populate the working directory 
    with the content of the commit and move HEAD to point to it
    
    checkout 
        1. travel conveniently in history
        2. allowing multiple branches of history
    """
    oid = get_oid(name)
    commit = get_commit(oid)
    read_tree(commit.tree, update_working=True)
    
    if is_branch(name):
        """
        HEAD = 'refs/heads/{name}', which makes HEAD point to branch
        when commit, update the object(oid) in bramch file along the symbolic ref 
        (in the function data._get_ref_internal(), change path 'HEAD' to the conent of HEAD file)
        """
        HEAD = data.RefValue(symbolic=True, value=f'refs/heads/{name}')
    else:
        """
        > Be careful about 'detached HEAD'
        commits will NOT advance the branch1, only HEAD will advance
        cause the symbolic is False
        """
        HEAD = data.RefValue(symbolic=False, value=oid)
    
    data.update_ref('HEAD', HEAD, deref=False)
        
def reset(oid):
    """
    make HEAD's symbolic ref point to oid(old)
    """
    
    data.update_ref('HEAD', data.RefValue(symbolic=False, value=oid))


def merge(other):
    """
    takes the tree of the HEAD and the tree of the branch that we want to merge with (+ common parent)
    calls base.read_tree_merged()
    (three-way merge + Fast-forward merge)
    """
    
    """
    three-way merge - an algorithm that merges two files using their common ancestor as a guide
    """
    
    HEAD = data.get_ref('HEAD').value
    assert HEAD
    # get the commen parent of Head and other
    merge_base = get_merge_base(other, HEAD)
    c_other = get_commit(other)

    # Handle fast-forward merge
    # If the common ancestor of HEAD and some-branch is HEAD itself it means that 
    # in particular HEAD is an ancestor of some-branch.
    if merge_base == HEAD:
        read_tree(c_other.tree, update_working=True)
        data.update_ref('HEAD',
                        data.RefValue(symbolic=False, value=other))
        print('Fast-forward merge, no need to commit')
        return
    
    # set MERGE_HEAD that point to the 'other commit'
    # The presence of MERGE_HEAD helps ugit that 
    # on the next commit is a merge commit with two parents - HEAD and MERGE_HEAD
    data.update_ref('MERGE_HEAD', data.RefValue(symbolic=False, value=other))

    c_base = get_commit(merge_base)
    c_HEAD = get_commit(HEAD)
    read_tree_merged(c_base.tree, c_HEAD.tree, c_other.tree, update_working=True)
    print('Merged in working tree\nPlease commit')

def get_merge_base (oid1, oid2):
    """
    find the common parent of oid1 and oid2
    :return: oid of the common parent
    """
    # save all parents of the first commit to a set
    parents1 = set(iter_commits_and_parents({oid1}))

    # iterate over the parents of the second commit in ancestry order until it reaches a parent 
    # that is a parent of the first commit
    for oid in iter_commits_and_parents({oid2}):
        if oid in parents1:
            return oid

def is_ancestor_of (commit, maybe_ancestor):
    return maybe_ancestor in iter_commits_and_parents ({commit})

def create_tag(name, oid):
    """
    create refs/tags/{name} ref to point to the desired OID
    """
    data.update_ref(f'refs/tags/{name}', data.RefValue (symbolic=False, value=oid))

def create_branch(name, oid):
    """
    record branch's oid in 'refs/heads/branch_name'
    """
    data.update_ref(f'refs/heads/{name}', data.RefValue (symbolic=False, value=oid))

def iter_branch_names():
    """
    iterate over all branch refs (like refs/heads/master) 
    and output just the name of the branch (like master)
    """
    for refname, _ in data.iter_refs('refs/heads/'):
        yield os.path.relpath(refname, 'refs/heads/')

def is_branch(branch):
    """
    if parameter 'branch' is branch
    """
    return data.get_ref(f'refs/heads/{branch}').value is not None

def get_branch_name():
    """
    return the current branch's name
    """
    HEAD = data.get_ref('HEAD', deref=False)
    if not HEAD.symbolic:
        return None
    # get branch's path
    HEAD = HEAD.value
    assert HEAD.startswith('refs/heads/')
    return os.path.relpath(HEAD, 'refs/heads')

Commit = namedtuple('Commit', ['tree', 'parents', 'message'])

def get_commit(oid):
    """
    parse a commit object by OID
    
    :return: 'Commit' tuple: tree, parent, message
    """
    #  merges two commits together, 
    # therefore the commit has two parent commits.
    parents = []
    
    commit = data.get_object(oid, 'commit').decode()
    lines = iter(commit.splitlines())
    # actually there are only two lines with oid: tree(current) and parent
    for line in itertools.takewhile(operator.truth, lines):
        key, value = line.split(' ', 1)
        if key == 'tree': # current
            tree = value
        elif key == 'parent': # parent
            parents.append(value)
        else:
            assert False, f'Unknown field {key}'
    
    # the commit object - two / one lines oid infromation = message
    message = '\n'.join(lines)
    return Commit(tree=tree, parents=parents, message=message)

def iter_commits_and_parents(oids):
    """
    get a list of all commits 
    and then recursively iterates on the trees in each commit
    """
    # Must yield the oid before acccessing it (to allow caller to fetch it
    # if needed)
    
    # use collections.deque instead of a set 
    # so that the order of commits is deterministic.
    oids = deque(oids)
    visited = set()
    
    while oids:
        oid = oids.popleft()
        if not oid or oid in visited:
            continue
        visited.add(oid)
        yield oid
    
        commit = get_commit(oid)
        # Return first parent next
        oids.appendleft(commit.parent[:1])
        # Return other parent next
        oid.appendleft(commit.parents[1:])

def iter_objects_in_commits (oids):
    """
    take a list of commit OIDs 
    and return all objects that are reachable from these commits
    """
    # Must yield the oid before acccessing it 
    # (to allow caller to fetch it if needed)

    visited = set()
    def iter_objects_in_tree(oid):
        visited.add(oid)
        yield oid
        for type_, oid, _ in _iter_tree_entries(oid):
            if oid not in visited:
                if type_ == 'tree':
                    yield from iter_objects_in_tree(oid)
                else:
                    visited.add(oid)
                    yield oid

    for oid in iter_commits_and_parents(oids):
        yield oid
        commit = get_commit(oid)
        if commit.tree not in visited:
            yield from iter_objects_in_tree(commit.tree)

def get_oid(name):
    """
    if name = type name return oid
    if name = oid return name(oid)
    """
    # make "@" be an alias for HEAD
    if name == '@': name = 'HEAD'
    # Name is ref(type name)
    refs_to_try = [
        # make command names shorter such as 'mytags
        # rather than spell out the full name of a tag (like refs/tags/mytag)
        f'{name}',
        f'refs/{name}',
        f'refs/tags/{name}',
        f'refs/heads/{name}'
    ]
    for ref in refs_to_try:
        if data.get_ref(ref, deref=False).value:
            return data.get_ref(ref).value
    
    # Name is SHA1 (oid)
    # hexdigits: "0123456789abcdefABCDEF"
    is_hex = all(c in string.hexdigits for c in name)
    if len(name) == 40 and is_hex:
        return name
    
    assert False, f'Unknown name {name}'

def add(filenames):
    """
    update index, which is a dictionary that maps filenames to their last remembered OID
    """
    def add_file(filename):
        """
        handle file path
        """
        # Normalize path
        filename = os.path.relpath(filename)
        with open(filename, 'rb') as f:
            oid = data.hash_object(f.read())
        index[filename] = oid
    
    def add_directory(dirname):
        """
        handle directory path
        """
        for root, _, filenames in os.walk(dirname):
            for filename in filenames:
                path = os.path.relpath(f'{root}/{filename}')
                if is_ignored(path) or not os.path.isfile(path):
                    continue
                add_file(path)
    
    with data.get_index() as index:
        for name in filenames:
            if os.path.isfile(name):
                add_file(name)
            elif os.path.isdir(name):
                add_directory(name)
        

def is_ignored(path):
    """
    ignore it the directory that isn't part of the user's files?
    """
    return '.ugit' in path.split('/')