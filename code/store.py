# =============================================================================
#
# EZID :: store.py
#
# Identifier metadata storage.  The store database serves as a backup
# for the primary "bind" noid database.  It also supports identifier
# harvesting.
#
# Note that the functions in this module are designed to hide errors
# from the caller by always returning successfully (but errors are
# still logged).
#
# N.B.: it is important that cursors be closed (and closed as quickly
# as possible), as they can possibly represent SHARED locks.
#
# Author:
#   Greg Janee <gjanee@ucop.edu>
#
# License:
#   Copyright (c) 2013, Regents of the University of California
#   http://creativecommons.org/licenses/BSD/
#
# -----------------------------------------------------------------------------

import sqlite3
import threading
import time
import uuid
import zlib

import config
import log
import util

# Notes on the database connection pool: The pool stores connections
# not currently in use.  The size of the pool is limited, but the
# total number of connections is not.  The pool is identified by a
# UUID; when EZID is reloaded, the pool is emptied and given a new
# UUID.  Any outstanding connections drawn from the old pool are not
# returned to the new pool, but are simply closed when returned.

_storeDatabase = None
_busySleep = None
_poolLock = threading.Lock()
_pool = None
_poolId = None
_poolLimit = None
_numConnections = None
_ownerMappingLock = threading.Lock()
_ownerMapping = None

def _loadConfig ():
  global _storeDatabase, _busySleep, _pool, _poolId, _poolLimit
  global _numConnections, _ownerMapping
  _busySleep = float(config.config("DEFAULT.sqlite_busy_sleep"))
  _poolLock.acquire()
  try:
    _storeDatabase = config.config("DEFAULT.store_database")
    _pool = []
    _poolId = uuid.uuid1()
    _poolLimit = int(config.config("DEFAULT.store_database_pool_limit"))
    _numConnections = 0
    _ownerMappingLock.acquire()
    try:
      _ownerMapping = None
    finally:
      _ownerMappingLock.release()
  finally:
    _poolLock.release()

_loadConfig()
config.addLoader(_loadConfig)

def setDatabase (file):
  """
  Sets the store database file.  This function is intended to be used
  by offline scripts only.  It must be called before any connections
  have been opened.
  """
  global _storeDatabase
  _storeDatabase = file

def _execute (cursor, statement, values=()):
  while True:
    try:
      cursor.execute(statement, values)
    except sqlite3.OperationalError, e:
      if e.message == "database is locked":
        time.sleep(_busySleep)
      else:
        raise e
    else:
      break

def _closeCursor (cursor):
  try:
    cursor.close()
  except Exception, e:
    log.otherError("store._closeCursor", e)
    return False
  else:
    return True

def _closeConnection (connection):
  try:
    connection.close()
  except:
    pass

def _loadOwnerKeys (connection):
  global _ownerMapping
  _ownerMappingLock.acquire()
  try:
    if _ownerMapping != None: return
    c = None
    try:
      c = connection.cursor()
      _execute(c, "SELECT owner, ownerKey FROM ownerMapping")
      _ownerMapping = dict(c.fetchall())
    except Exception, e:
      log.otherError("store._loadOwnerKeys", e)
      raise e
    finally:
      if c: _closeCursor(c)
  finally:
    _ownerMappingLock.release()

def _getConnection ():
  global _numConnections
  _poolLock.acquire()
  try:
    if len(_pool) > 0:
      return (_pool.pop(), _poolId)
    else:
      # Turn on auto-commit, so that a BEGIN statement can disable it
      # (sounds paradoxical, but that's how SQLite works).
      # Setting check_same_thread to false allows connections to be
      # used by different threads.  The almost nonexistent
      # documentation on this flag is confusing.  Either the check
      # enabled by this flag is not necessary at all, given the
      # version of SQLite we're using (3.7), or it's not necessary
      # given our thread/connection controls (which ensure that a
      # connection is used by only one thread at a time, and a
      # connection is returned to the pool only if and when no
      # transaction is in progress).
      c = sqlite3.connect(_storeDatabase, isolation_level=None,
        check_same_thread=False)
      try:
        _loadOwnerKeys(c)
      except Exception, e:
        _closeConnection(c)
        raise e
      _numConnections += 1
      return (c, _poolId)
  finally:
    _poolLock.release()

def _returnConnection (connection, poolId, tainted=False):
  global _numConnections
  _poolLock.acquire()
  try:
    if poolId == _poolId:
      if tainted or len(_pool) == _poolLimit:
        _numConnections -= 1
        closeConnection = True
      else:
        _pool.append(connection)
        closeConnection = False
    else:
      closeConnection = True
  finally:
    _poolLock.release()
  if closeConnection: _closeConnection(connection)

def numConnections ():
  """
  Returns a tuple (number of open database connections, number of
  connections in active use).
  """
  _poolLock.acquire()
  try:
    return (_numConnections, _numConnections-len(_pool))
  finally:
    _poolLock.release()

def _begin (cursor):
  _execute(cursor, "BEGIN IMMEDIATE")
  return True

def _commit (cursor):
  _execute(cursor, "COMMIT")

def _rollback (cursor):
  try:
    _execute(cursor, "ROLLBACK")
  except Exception, e:
    log.otherError("store._rollback", e)

def _getOwnerKey (owner, connection):
  _ownerMappingLock.acquire()
  try:
    assert _ownerMapping is not None,\
      "no owner mapping (race condition with recent configuration reload)"
    if owner in _ownerMapping: return _ownerMapping[owner]
    c = None
    begun = False
    try:
      c = connection.cursor()
      begun = _begin(c)
      if len(_ownerMapping) == 0:
        key = 1
      else:
        key = max(_ownerMapping.values()) + 1
      _execute(c, "INSERT INTO ownerMapping (ownerKey, owner) VALUES (?, ?)",
        (key, owner))
      _commit(c)
      _ownerMapping[owner] = key
      return key
    except Exception, e:
      log.otherError("store._getOwnerKey", e)
      if begun: _rollback(c)
      raise e
    finally:
      if c: _closeCursor(c)
  finally:
    _ownerMappingLock.release()

def _blobify (metadata):
  return zlib.compress(" ".join("%s %s" % (util.encode4(k), util.encode3(v))\
    for k, v in metadata.items()))

def _deblobify (blob):
  v = zlib.decompress(blob).split(" ")
  d = {}
  for i in range(0, len(v), 2): d[util.decode(v[i])] = util.decode(v[i+1])
  return d

def insert (identifier, metadata):
  """
  Inserts an identifier in the store database.  'identifier' should be
  an unqualified ARK identifier, e.g., "13030/foo".  (To insert a
  non-ARK identifier, insert the identifier's shadow ARK.)  'metadata'
  should be the identifier's complete metadata as a dictionary of
  element (name, value) pairs.
  """
  connection = None
  tainted = False
  c = None
  begun = False
  try:
    connection, poolId = _getConnection()
    key = _getOwnerKey(metadata["_o"], connection)
    updateTime = int(metadata["_su"] if "_su" in metadata else metadata["_u"])
    c = connection.cursor()
    begun = _begin(c)
    _execute(c, "INSERT INTO identifier (identifier, ownerKey, updateTime, " +\
      "metadata) VALUES (?, ?, ?, ?)",
      (identifier, key, updateTime, buffer(_blobify(metadata))))
    _commit(c)
  except Exception, e:
    log.otherError("store.insert", e)
    tainted = True
    if begun: _rollback(c)
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)

def update (identifier, metadata, insertIfNecessary=False):
  """
  Updates an identifier in the store database.  If 'insertIfNecessary'
  is true, the identifier is inserted if not already in the database.
  'identifier' should be an unqualified ARK identifier, e.g.,
  "13030/foo".  (To update a non-ARK identifier, reference the
  identifier by its shadow ARK.)  'metadata' should be the
  identifier's complete metadata as a dictionary of element (name,
  value) pairs.
  """
  connection = None
  tainted = False
  c = None
  begun = False
  try:
    connection, poolId = _getConnection()
    key = _getOwnerKey(metadata["_o"], connection)
    updateTime = int(metadata["_su"] if "_su" in metadata else metadata["_u"])
    c = connection.cursor()
    begun = _begin(c)
    # N.B.: A race condition can't occur here thanks to the global
    # identifier lock in ezid.py.
    if insertIfNecessary:
      _execute(c, "SELECT COUNT(*) FROM identifier WHERE identifier = ?",
        (identifier,))
      doInsert = (c.fetchone()[0] == 0)
    else:
      doInsert = False
    if doInsert:
      _execute(c, "INSERT INTO identifier (identifier, ownerKey, " +\
        "updateTime, metadata) VALUES (?, ?, ?, ?)",
        (identifier, key, updateTime, buffer(_blobify(metadata))))
    else:
      _execute(c, "UPDATE identifier SET ownerKey = ?, updateTime = ?, " +\
        "metadata = ? WHERE identifier = ?",
        (key, updateTime, buffer(_blobify(metadata)), identifier))
    _commit(c)
  except Exception, e:
    log.otherError("store.update", e)
    tainted = True
    if begun: _rollback(c)
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)

def exists (identifier):
  """
  Returns true if an identifier exists in the store database.
  'identifier' should be an unqualified ARK identifier, e.g.,
  "13030/foo".  (To check the existence of a non-ARK identifier, check
  the identifier's shadow ARK.)
  """
  connection = None
  tainted = False
  c = None
  try:
    connection, poolId = _getConnection()
    c = connection.cursor()
    _execute(c, "SELECT COUNT(*) FROM identifier WHERE identifier = ?",
      (identifier,))
    return (c.fetchone()[0] != 0)
  except Exception, e:
    log.otherError("store.exists", e)
    tainted = True
    return False
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)

def get (identifier):
  """
  Returns the metadata for an identifier as a dictionary of element
  (name, value) pairs, or returns None if the identifier doesn't exist
  in the store database.  'identifier' should be an unqualified ARK
  identifier, e.g., "13030/foo".  (To get a non-ARK identifier's
  metadata, reference the identifier by its shadow ARK.)
  """
  connection = None
  tainted = False
  c = None
  try:
    connection, poolId = _getConnection()
    c = connection.cursor()
    _execute(c, "SELECT metadata FROM identifier WHERE identifier = ?",
      (identifier,))
    r = c.fetchall()
    if len(r) > 0:
      return _deblobify(r[0][0])
    else:
      return None
  except Exception, e:
    log.otherError("store.get", e)
    tainted = True
    return None
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)

def harvest (owner=None, since=None, start=None, maximum=None):
  """
  Returns a list of all identifiers in the store database in
  lexicographic order; each identifier is returned as a tuple
  (identifier, metadata).  In the tuple, 'identifier' is an
  unqualified ARK and metadata is a dictionary of element (name,
  value) pairs.  If 'owner' is supplied, it should be a user
  persistent identifier (e.g., "ark:/99166/foo"), and only that user's
  identifiers are returned.  If 'since' is supplied, it should be a
  Unix timestamp, and only those identifiers updated more recently
  than that are returned.  If 'start' is supplied, it should be a
  previously-returned identifier, and only identifiers
  lexicographically greater than it are returned.  'maximum' can be
  used to limit the number of identifiers returned.  To obtain all
  identifiers most efficiently and placing the least contention on the
  store database, supply a maximum and use the last returned
  identifier as the 'start' argument of the next call, with the loop
  terminating when no identifiers are returned.
  """
  connection = None
  tainted = False
  c = None
  try:
    connection, poolId = _getConnection()
    constraints = []
    values = []
    if owner != None:
      constraints.append(" ownerKey = ?")
      values.append(_getOwnerKey(owner, connection))
    if start != None:
      constraints.append(" identifier > ?")
      values.append(start)
    if since != None:
      constraints.append(" updateTime > ?")
      values.append(since)
    constraints = " AND".join(constraints)
    if constraints != "": constraints = " WHERE" + constraints
    if maximum != None:
      limit = " LIMIT ?"
      values.append(maximum)
    else:
      limit = ""
    c = connection.cursor()
    _execute(c, ("SELECT identifier, metadata FROM identifier%s " +\
      "ORDER BY identifier%s") % (constraints, limit), tuple(values))
    return [(i, _deblobify(m)) for i, m in c.fetchall()]
  except Exception, e:
    log.otherError("store.harvest", e)
    tainted = True
    return []
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)

def delete (identifier):
  """
  Deletes an identifier from the store database.  'identifier' should
  be an unqualified ARK identifier, e.g., "13030/foo".  (To delete a
  non-ARK identifier, delete the identifier's shadow ARK.)
  """
  connection = None
  tainted = False
  c = None
  begun = False
  try:
    connection, poolId = _getConnection()
    c = connection.cursor()
    begun = _begin(c)
    _execute(c, "DELETE FROM identifier WHERE identifier = ?", (identifier,))
    _commit(c)
  except Exception, e:
    log.otherError("store.delete", e)
    tainted = True
    if begun: _rollback(c)
  finally:
    if c:
      if not _closeCursor(c): tainted = True
    if connection: _returnConnection(connection, poolId, tainted)