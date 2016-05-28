# =============================================================================
#
# EZID :: download.py
#
# Batch download.
#
# Downloads are created by a single daemon thread.  The download
# creation process is designed to be restartable at any point: if the
# server is restarted, the current download resumes where it left off.
#
# When the server is reloaded, a new daemon thread gets created.  Race
# conditions exist between the old and new threads while the old
# thread still exists, but actual conflicts should be very unlikely.
#
# Author:
#   Greg Janee <gjanee@ucop.edu>
#
# License:
#   Copyright (c) 2015, Regents of the University of California
#   http://creativecommons.org/licenses/BSD/
#
# -----------------------------------------------------------------------------

import calendar
import csv
import django.conf
import django.core.mail
import hashlib
import os
import os.path
import re
import subprocess
import threading
import time
import uuid
import xml.sax.saxutils

import anvl
import config
import ezid
import ezidapp.models
import log
import mapping
import policy
import store
import util2
from django.utils.translation import ugettext as _

_ezidUrl = None
_usedFilenames = None
_lock = threading.Lock()
_daemonEnabled = None
_threadName = None
_idleSleep = None
_gzipCommand = None
_zipCommand = None

def _loadConfig ():
  global _ezidUrl, _usedFilenames, _daemonEnabled, _threadName, _idleSleep
  global _gzipCommand, _zipCommand
  _ezidUrl = config.get("DEFAULT.ezid_base_url")
  _lock.acquire()
  try:
    if _usedFilenames == None:
      _usedFilenames = [r.filename for r in\
        ezidapp.models.DownloadQueue.objects.all()] +\
        [f.split(".")[0] for f in\
        os.listdir(django.conf.settings.DOWNLOAD_PUBLIC_DIR)]
  finally:
    _lock.release()
  _idleSleep = int(config.get("daemons.download_processing_idle_sleep"))
  _gzipCommand = config.get("DEFAULT.gzip_command")
  _zipCommand = config.get("DEFAULT.zip_command")
  _daemonEnabled = (django.conf.settings.DAEMON_THREADS_ENABLED and\
    config.get("daemons.download_enabled").lower() == "true")
  if _daemonEnabled:
    _threadName = uuid.uuid1().hex
    t = threading.Thread(target=_daemonThread, name=_threadName)
    t.setDaemon(True)
    t.start()

_formatCode = {
  "anvl": ezidapp.models.DownloadQueue.ANVL,
  "csv": ezidapp.models.DownloadQueue.CSV,
  "xml": ezidapp.models.DownloadQueue.XML
}

_formatSuffix = {
  ezidapp.models.DownloadQueue.ANVL: "txt",
  ezidapp.models.DownloadQueue.CSV: "csv",
  ezidapp.models.DownloadQueue.XML: "xml"
}

_compressionCode = {
  "gzip": ezidapp.models.DownloadQueue.GZIP,
  "zip": ezidapp.models.DownloadQueue.ZIP
}

def _oneline (s):
  return re.sub("\s", " ", s)

class _ValidationException (Exception):
  pass

def _validateString (v):
  s = v.strip()
  if s == "": raise _ValidationException("empty value")
  return s

def _validateEnumerated (v, l):
  if v not in l: raise _ValidationException("invalid parameter value")
  return v

def _validateBoolean (v):
  return (_validateEnumerated(v, ["yes", "no"]) == "yes")

def _validateTimestamp (v):
  try:
    try:
      return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ"))
    except:
      return int(v)
  except:
    raise _ValidationException("invalid timestamp")

def _validateUser (v):
  u = ezidapp.models.getUserByUsername(v)
  if u != None and not u.isAnonymous:
    return u
  else:
    raise _ValidationException("no such user")

def _validateGroup (v):
  g = ezidapp.models.getGroupByGroupname(v)
  if g != None and not g.isAnonymous:
    return g
  else:
    raise _ValidationException("no such group")

# A simple encoding mechanism for storing Python objects as strings
# follows.  We could use pickling, but this technique makes debugging
# a little easier.

def _escape (s):
  return re.sub("[%,=]", lambda c: "%%%02X" % ord(c.group(0)), s)

def _encode (o):
  if type(o) is bool:
    return "B" + str(o)
  elif type(o) is int:
    return "I" + str(o)
  elif type(o) in [str, unicode]:
    return "S" + o
  elif type(o) is list:
    return "L" + ",".join(map(lambda i: _escape(_encode(i)), o))
  elif type(o) is dict:
    return "D" + ",".join(map(lambda kv: "%s=%s" % (_escape(_encode(kv[0])),
      _escape(_encode(kv[1]))), o.items()))
  else:
    assert False, "unhandled case"

def _unescape (s):
  return re.sub("%([0-9A-F][0-9A-F])", lambda m: chr(int(m.group(1), 16)), s)

def _decode (s):
  if s[0] == "B":
    return (s[1:] == "True")
  elif s[0] == "I":
    return int(s[1:])
  elif s[0] == "S":
    return s[1:]
  elif s[0] == "L":
    if len(s) > 1:
      return map(lambda i: _decode(_unescape(i)), s[1:].split(","))
    else:
      return []
  elif s[0] == "D":
    if len(s) > 1:
      return dict(map(lambda i: tuple(map(lambda kv: _decode(_unescape(kv)),
        i.split("="))), s[1:].split(",")))
    else:
      return {}
  else:
    assert False, "unhandled case"

_parameters = {
  # name: (repeatable, validator)
  "column": (True, _validateString),
  "convertTimestamps": (False, _validateBoolean),
  "createdAfter": (False, _validateTimestamp),
  "createdBefore": (False, _validateTimestamp),
  "crossref": (False, _validateBoolean),
  "exported": (False, _validateBoolean),
  "format": (False, lambda v: _validateEnumerated(v, ["anvl", "csv", "xml"])),
  "compression": (False, lambda v: _validateEnumerated(v, ["gzip", "zip"])),
  "notify": (True, _validateString),
  "owner": (True, _validateUser),
  "ownergroup": (True, _validateGroup),
  "permanence": (False, lambda v: _validateEnumerated(v, ["test", "real"])),
  "profile": (True, _validateString),
  "status": (True, lambda v: _validateEnumerated(v, ["reserved", "public",
    "unavailable"])),
  "type": (True, lambda v: _validateEnumerated(v, ["ark", "doi", "urn"])),
  "updatedAfter": (False, _validateTimestamp),
  "updatedBefore": (False, _validateTimestamp)
}

def _generateFilename (requestor):
  while True:
    f = hashlib.sha1("%s,%s,%s" % (requestor, str(time.time()),
      django.conf.settings.SECRET_KEY)).hexdigest()[::4]
    _lock.acquire()
    try:
      if f not in _usedFilenames:
        _usedFilenames.append(f)
        return f
    finally:
      _lock.release()

def enqueueRequest (user, request):
  """
  Enqueues a batch download request.  The request must be
  authenticated; 'user' should be a StoreUser object.  'request'
  should be a django.http.QueryDict object (from a POST request or
  manually created) containing the parameters of the request.  The
  available parameters are described in the API documentation.  One
  feature not mentioned in the documentation: for the 'notify'
  parameter, an email address may be a straight address
  ("fred@slate.com") or may include an addressee name ("Fred
  Flintstone <fred@slate.com>"); in the latter case a salutation line
  will be added to the email message.

  The successful return is a string that includes the download URL, as
  in:

    success: http://ezid.cdlib.org/download/da543b91a0.xml.gz

  Unsuccessful returns include the strings:

    error: forbidden
    error: bad request - subreason...
    error: internal server error
  """
  def error (s):
    return "error: bad request - " + s
  try:
    d = {}
    for k in request:
      if k not in _parameters:
        return error("invalid parameter: " + _oneline(k))
      try:
        if _parameters[k][0]:
          d[k] = map(_parameters[k][1], request.getlist(k))
        else:
          if len(request.getlist(k)) > 1:
            return error("parameter is not repeatable: " + k)
          d[k] = _parameters[k][1](request[k])
      except _ValidationException, e:
        return error("parameter '%s': %s" % (k, str(e)))
    if "format" not in d:
      return error("missing required parameter: format")
    format = d["format"]
    del d["format"]
    if "compression" in d:
      compression = d["compression"]
      del d["compression"]
    else:
      compression = "gzip"
    if format == "csv":
      if "column" not in d:
        return error("format 'csv' requires at least one column")
      columns = d["column"]
      del d["column"]
    else:
      if "column" in d:
        return error("parameter is incompatible with format: column")
      columns = []
    toHarvest = []
    if "owner" in d:
      for o in d["owner"]:
        if not policy.authorizeDownload(user, owner=o):
          return "error: forbidden"
        if o.pid not in toHarvest: toHarvest.append(o.pid)
      del d["owner"]
    if "ownergroup" in d:
      for g in d["ownergroup"]:
        if not policy.authorizeDownload(user, ownergroup=g):
          return "error: forbidden"
        for u in g.users.all():
          if u.pid not in toHarvest: toHarvest.append(u.pid)
      del d["ownergroup"]
    if len(toHarvest) == 0: toHarvest = [user.pid]
    if "notify" in d:
      notify = d["notify"]
      del d["notify"]
    else:
      notify = []
    if "convertTimestamps" in d:
      options = { "convertTimestamps": d["convertTimestamps"] }
      del d["convertTimestamps"]
    else:
      options = { "convertTimestamps": False }
    requestor = user.pid
    filename = _generateFilename(requestor)
    r = ezidapp.models.DownloadQueue(requestTime=int(time.time()),
      rawRequest=request.urlencode(),
      requestor=requestor, format=_formatCode[format],
      compression=_compressionCode[compression],
      columns=_encode(columns), constraints=_encode(d),
      options=_encode(options), notify=_encode(notify), filename=filename,
      toHarvest=",".join(toHarvest))
    r.save()
    return "success: %s/download/%s.%s" % (_ezidUrl, filename,
      _fileSuffix(r))
  except Exception, e:
    log.otherError("download.enqueueRequest", e)
    return "error: internal server error"

def getQueueLength ():
  """
  Returns the length of the batch download queue.
  """
  return ezidapp.models.DownloadQueue.objects.count()

class _AbortException (Exception):
  pass

def _checkAbort ():
  # This function provides a handy way to abort processing if the
  # daemon is disabled or if a new daemon thread is started by a
  # configuration reload.  It doesn't entirely eliminate potential
  # race conditions between two daemon threads, but it should make
  # conflicts very unlikely.
  if not _daemonEnabled or threading.currentThread().getName() != _threadName:
    raise _AbortException()

def _wrapException (context, exception):
  m = str(exception)
  if len(m) > 0: m = ": " + m
  return Exception("batch download error: %s: %s%s" % (context,
    type(exception).__name__, m))

def _fileSuffix (r):
  if r.compression == ezidapp.models.DownloadQueue.GZIP:
    return _formatSuffix[r.format] + ".gz"
  else:
    return "zip"

def _path (r, i):
  # i=1: uncompressed work file
  # i=2: compressed work file
  # i=3: compressed delivery file
  # i=4: request sidecar file
  if i in [1, 2]:
    d = django.conf.settings.DOWNLOAD_WORK_DIR
  else:
    d = django.conf.settings.DOWNLOAD_PUBLIC_DIR
  if i == 1:
    s = _formatSuffix[r.format]
  elif i in [2, 3]:
    s = _fileSuffix(r)
  else:
    s = "request"
  return os.path.join(d, "%s.%s" % (r.filename, s))

def _csvEncode (s):
  return _oneline(s).encode("UTF-8")

def _flushFile (f):
  f.flush()
  os.fsync(f.fileno())

def _createFile (r):
  f = None
  try:
    f = open(_path(r, 1), "wb")
    if r.format == ezidapp.models.DownloadQueue.CSV:
      w = csv.writer(f)
      w.writerow([_csvEncode(c) for c in _decode(r.columns)])
      _flushFile(f)
    elif r.format == ezidapp.models.DownloadQueue.XML:
      f.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<records>")
      _flushFile(f)
    # We don't know exactly what the CSV writer wrote, so we must
    # probe the file to find its size.
    n = f.tell()
  except Exception, e:
    raise _wrapException("error creating file", e)
  else:
    r.stage = ezidapp.models.DownloadQueue.HARVEST
    r.fileSize = n
    r.save()
  finally:
    if f: f.close()

def _satisfiesConstraints (id, record, constraints):
  for k, v in constraints.items():
    if k == "createdAfter":
      if int(record["_created"]) < v: return False
    elif k == "createdBefore":
      if int(record["_created"]) >= v: return False
    elif k == "crossref":
      c = record.get("_crossref", "no").startswith("yes")
      if v^c: return False
    elif k == "exported":
      e = (record["_export"] == "yes")
      if v^e: return False
    elif k == "permanence":
      if (v == "test") ^ util2.isTestIdentifier(id): return False
    elif k == "profile":
      if record["_profile"] not in v: return False
    elif k == "status":
      s = record["_status"]
      if s.startswith("unavailable"): s = "unavailable"
      if s not in v: return False
    elif k == "type":
      if id.split(":", 1)[0] not in v: return False
    elif k == "updatedAfter":
      if int(record["_updated"]) < v: return False
    elif k == "updatedBefore":
      if int(record["_updated"]) >= v: return False
    else:
      assert False, "unhandled case"
  return True

def _writeAnvl (f, id, record):
  if f.tell() > 0: f.write("\n")
  f.write(":: %s\n" % id)
  f.write(anvl.format(record).encode("UTF-8"))

_mappedFields = set(["_mappedCreator", "_mappedTitle", "_mappedPublisher",
  "_mappedDate", "_mappedType"])

def _writeCsv (f, id, record, columns):
  w = csv.writer(f)
  l = []
  km = None
  for c in columns:
    if c == "_id":
      l.append(id)
    elif c in _mappedFields:
      if km == None: km = mapping.map(record)
      v = getattr(km, c[7:].lower())
      l.append(v if v != None else "")
    else:
      l.append(record.get(c, ""))
  w.writerow([_csvEncode(c) for c in l])

def _xmlEscape (s):
  return xml.sax.saxutils.escape(s, { "\"": "&quot;" })

_prologRE = re.compile("<\?xml\s+version\s*=\s*['\"][-\w.:]+[\"']" +\
  "(\s+encoding\s*=\s*['\"][-\w.]+[\"'])?" +\
  "(\s+standalone\s*=\s*['\"](yes|no)[\"'])?\s*\?>\s*")

def _removeProlog (document):
  m = _prologRE.match(document)
  if m:
    return document[len(m.group(0)):]
  else:
    return document

def _writeXml (f, id, record):
  f.write("<record identifier=\"%s\">" % _xmlEscape(id))
  for k, v in record.items():
    if k in ["datacite", "crossref"]:
      v = _removeProlog(v)
    else:
      v = _xmlEscape(v)
    f.write(("<element name=\"%s\">%s</element>" %\
      (_xmlEscape(k), v)).encode("UTF-8"))
  f.write("</record>")

def _harvest1 (r, f):
  columns = _decode(r.columns)
  constraints = _decode(r.constraints)
  options = _decode(r.options)
  while True:
    _checkAbort()
    ids = store.harvest(owner=r.toHarvest.split(",")[r.currentIndex],
      start=r.lastId, maximum=1000)
    if len(ids) == 0: break
    try:
      for id, record in ids:
        nqidentifier = record.get("_s", "ark:/" + id)
        ezid.convertMetadataDictionary(record, id)
        if _satisfiesConstraints(nqidentifier, record, constraints):
          if options["convertTimestamps"]:
            record["_created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
              time.gmtime(int(record["_created"])))
            record["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
              time.gmtime(int(record["_updated"])))
          _checkAbort()
          if r.format == ezidapp.models.DownloadQueue.ANVL:
            _writeAnvl(f, nqidentifier, record)
          elif r.format == ezidapp.models.DownloadQueue.CSV:
            _writeCsv(f, nqidentifier, record, columns)
          elif r.format == ezidapp.models.DownloadQueue.XML:
            _writeXml(f, nqidentifier, record)
          else:
            assert False, "unhandled case"
      _checkAbort()
      _flushFile(f)
    except _AbortException:
      raise
    except Exception, e:
      raise _wrapException("error writing file", e)
    r.lastId = ids[-1][0]
    r.fileSize = f.tell()
    r.save()

def _harvest (r):
  f = None
  try:
    try:
      assert os.path.getsize(_path(r, 1)) >= r.fileSize, "file is short"
      f = open(_path(r, 1), "r+b")
      f.seek(r.fileSize)
      f.truncate()
    except Exception, e:
      raise _wrapException("error re-opening/seeking/truncating file", e)
    start = r.currentIndex
    for i in range(r.currentIndex, len(r.toHarvest.split(","))):
      _checkAbort()
      if i > start:
        r.currentIndex = i
        r.lastId = ""
        r.save()
      _harvest1(r, f)
    _checkAbort()
    if r.format == ezidapp.models.DownloadQueue.XML:
      try:
        f.write("</records>")
        _flushFile(f)
      except Exception, e:
        raise _wrapException("error writing file footer", e)
    r.stage = ezidapp.models.DownloadQueue.COMPRESS
    r.save()
  finally:
    if f: f.close()

def _compressFile (r):
  infile = None
  outfile = None
  try:
    # The compression command may be long-lived, and a new daemon
    # thread may be created by a server restart or reload while it is
    # still running, in which case we don't try to kill the old
    # process, but simply delete its output file and let it die a
    # natural death.
    if os.path.exists(_path(r, 2)): os.unlink(_path(r, 2))
    if r.compression == ezidapp.models.DownloadQueue.GZIP:
      infile = open(_path(r, 1))
      outfile = open(_path(r, 2), "w")
      p = subprocess.Popen([_gzipCommand], stdin=infile, stdout=outfile,
        stderr=subprocess.PIPE, close_fds=True, env={})
      stderr = p.communicate()[1]
    else:
      p = subprocess.Popen([_zipCommand, "-jq", _path(r, 2), _path(r, 1)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, close_fds=True, env={})
      stderr = p.communicate()[0]
    _checkAbort()
    assert p.returncode == 0 and stderr == "",\
      "compression command returned status code %d, stderr '%s'" %\
      (p.returncode, stderr)
  except _AbortException:
    raise
  except Exception, e:
    raise _wrapException("error compressing file", e)
  else:
    r.stage = ezidapp.models.DownloadQueue.DELETE
    r.save()
  finally:
    if infile: infile.close()
    if outfile: outfile.close()

def _deleteUncompressedFile (r):
  try:
    if os.path.exists(_path(r, 1)): os.unlink(_path(r, 1))
  except Exception, e:
    raise _wrapException("error deleting uncompressed file", e)
  else:
    r.stage = ezidapp.models.DownloadQueue.MOVE
    r.save()

def _moveCompressedFile (r):
  try:
    if os.path.exists(_path(r, 2)):
      os.rename(_path(r, 2), _path(r, 3))
    else:
      assert os.path.exists(_path(r, 3)), "file has disappeared"
  except Exception, e:
    raise _wrapException("error moving compressed file", e)
  else:
    r.stage = ezidapp.models.DownloadQueue.NOTIFY
    r.save()

def _notifyRequestor (r):
  f = None
  try:
    f = open(_path(r, 4), "w")
    f.write("%s\n%s\n" % (ezidapp.models.getUserByPid(r.requestor).username,
      r.rawRequest.encode("UTF-8")))
  except Exception, e:
    raise _wrapException("error writing sidecar file", e)
  finally:
    if f: f.close()
  for emailAddress in _decode(r.notify):
    m = re.match("(.*)<([^>]*)>$", emailAddress)
    if m and m.group(1).strip() != "" and m.group(2).strip() != "":
      salutation = _("Dear %(recipient)s,\n\n") % {'recipient': m.group(1).strip()}
      emailAddress = m.group(2).strip()
    else:
      salutation = ""
    message = salutation + _("Thank you for using EZID ") +\
      _("to easily create and manage your identifiers. ")
    message += (_("The batch download you requested is available at:\n\n") +\
      "%s/download/%s.%s\n\n" +\
      _("The download will be deleted in 1 week.\n") +\
      _("This is an automated email.  Please do not reply.\n")) %\
      (_ezidUrl, r.filename, _fileSuffix(r))
    try:
      django.core.mail.send_mail(_("Your EZID Batch Download Link"), message,
        django.conf.settings.SERVER_EMAIL, [emailAddress], fail_silently=True)
    except Exception, e:
      raise _wrapException(_("error sending email"), e)
  r.delete()

def _daemonThread ():
  doSleep = True
  while True:
    if doSleep:
      django.db.connections["default"].close()
      time.sleep(_idleSleep)
    try:
      _checkAbort()
      r = ezidapp.models.DownloadQueue.objects.all().order_by("seq")[:1]
      if len(r) == 0:
        doSleep = True
        continue
      r = r[0]
      _checkAbort()
      if r.stage == ezidapp.models.DownloadQueue.CREATE:
        _createFile(r)
      elif r.stage == ezidapp.models.DownloadQueue.HARVEST:
        _harvest(r)
      elif r.stage == ezidapp.models.DownloadQueue.COMPRESS:
        _compressFile(r)
      elif r.stage == ezidapp.models.DownloadQueue.DELETE:
        _deleteUncompressedFile(r)
      elif r.stage == ezidapp.models.DownloadQueue.MOVE:
        _moveCompressedFile(r)
      elif r.stage == ezidapp.models.DownloadQueue.NOTIFY:
        _notifyRequestor(r)
      else:
        assert False, "unhandled case"
      doSleep = False
    except _AbortException:
      break
    except Exception, e:
      log.otherError("download._daemonThread", e)
      doSleep = True

_loadConfig()
config.registerReloadListener(_loadConfig)
