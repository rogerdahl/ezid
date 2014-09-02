import ui_common as uic
from django.shortcuts import redirect
import django.contrib.messages
import metadata
import ezid
import ui_create
import urllib
import datacite_xml
import os
import re
import policy
from lxml import etree
def index(request):
  d = { 'menu_item' : 'ui_demo.index' }
  return redirect("ui_demo.simple")
  return uic.render(request, 'create/index', d)

def simple(request):
  d = { 'menu_item' :'ui_demo.simple' }
  d["testPrefixes"] = uic.testPrefixes
  d['prefixes'] = sorted(uic.testPrefixes, key=lambda p: p['namespace'].lower()) #must be done before calliung form processing
  r = ui_create.simple_form_processing(request, d)
  if r == 'bad_request':
    uic.badRequest()
  elif r.startswith('created_identifier:'):
    return redirect("/id/" + urllib.quote(r.split()[1], ":/"))
  else:
    return uic.render(request, 'demo/simple', d)

def advanced(request):
  d = { 'menu_item' : 'ui_demo.advanced' }
  d["testPrefixes"] = uic.testPrefixes
  d['prefixes'] = sorted(uic.testPrefixes, key=lambda p: p['namespace'].lower()) #must be done before calling form processing
  r = ui_create.advanced_form_processing(request, d)
  if r == 'bad_request':
    uic.badRequest()
  elif r.startswith('created_identifier:'):
    return redirect("/id/" + urllib.quote(r.split()[1], ":/"))
  else:
    return uic.render(request, 'demo/advanced', d)
  
def ajax_advanced(request):
  """Takes the request and processes create datacite advanced (xml) form
  from both create/demo and edit areas"""
  if request.is_ajax():
    d = {}
    error_msgs = []
    if (request.POST['action'] == 'create'):
      required = ['shoulder', 'remainder', '_target', 'publish', 'export']
      action_result = ['creating', 'created']
    else:   # action='edit'
      required = ['_target', '_export']
      action_result = ['editing', 'edited successfully']
      if not request.POST['identifier']:
        error_msgs.append("Unable to edit. Identifier not supplied.")
    d["testPrefixes"] = uic.testPrefixes
    if 'auth' in request.session:
      d['prefixes'] = sorted([{ "namespace": s.name, "prefix": s.key }\
        for s in policy.getShoulders(request.session["auth"].user,
        request.session["auth"].group)],
        key=lambda p: (p['namespace'] + ' ' + p['prefix']).lower())
    else:
      d['prefixes'] = []
    pre_list = [p['prefix'] for p in d['prefixes'] + d['testPrefixes']]
    if (request.POST['action'] == 'create' and\
        request.POST['shoulder'] not in pre_list):
        error_msgs.append("Unauthorized to create with this identifier prefix.")
    for x in required:
      if x not in request.POST:
        error_msgs.append("A required form element was not submitted.")
        return uic.jsonResponse({'status': 'failure', 'errors': error_msgs })

    error_msgs = error_msgs + uic.validate_advanced_top(request)
    for k, v in {'/resource/creators/creator[1]/creatorName': 'creator name',
                 '/resource/titles/title[1]': 'title',
                 '/resource/publisher': 'publisher',
                 '/resource/publicationYear': 'publication year'}.items():
      if (not (k in request.POST)) or request.POST[k].strip() == '':
        error_msgs.append("Please enter a " + v)
    
    if ('/resource/publicationYear' in request.POST) and \
              not re.compile('^\d{4}$').match(request.POST['/resource/publicationYear']):
      error_msgs.append("Please enter a four digit year for the publication year.")
      
    #for k, v in request.POST.iteritems():
    #  if v:
    #    if re.match(r'^/resource/dates/date\[\d+?\]$', k ) and not re.match(r'^\d{4}', v ):
    #      error_msgs.append("Please ensure your date is numeric and in the correct format.")
    
    if len(error_msgs) > 0:
      return uic.jsonResponse({'status': 'failure', 'errors': error_msgs })
    
    return_val = datacite_xml.generate_xml(request.POST)
    xsd_path = django.conf.settings.PROJECT_ROOT + "/xsd/datacite3-kernel/metadata.xsd"
    if datacite_xml.validate_document(return_val, xsd_path, error_msgs) == False:
      return uic.jsonResponse({'status': 'failure', 'errors': error_msgs })

    if (request.POST['action'] == 'edit'): 
      if request.POST['_status'] == 'unavailable':
        stts = request.POST['_status'] + " | " + request.POST['stat_reason']
      else:
        stts = request.POST['_status']
      to_write = _assembleMetadata(request, stts, return_val) 
      s = ezid.setMetadata(request.POST['identifier'], uic.user_or_anon_tup(request),\
          uic.group_or_anon_tup(request), to_write)
    else:  # action=='create'
      stts = ("public" if request.POST["publish"] == "True" else "reserved")
      to_write = _assembleMetadata(request, stts, return_val) 
      
      #write out ID and metadata (one variation with special remainder, one without)
      if request.POST['remainder'] == '' or\
         request.POST['remainder'] == uic.remainder_box_default:
        s = ezid.mintIdentifier(request.POST['shoulder'], uic.user_or_anon_tup(request), 
          uic.group_or_anon_tup(request), to_write)
      else:
        s = ezid.createIdentifier(request.POST['shoulder'] +\
            request.POST['remainder'], uic.user_or_anon_tup(request),
        uic.group_or_anon_tup(request), to_write)

    if s.startswith("success:"):
      new_id = s.split()[1]
      django.contrib.messages.success(request, "Identifier " + action_result[1] + ".")
      return uic.jsonResponse({'status': 'success', 'id': new_id })
    else:
      return uic.jsonResponse({'status': 'failure', 'errors': ["There was an error " +
        action_result[0] + " your identifier:"  + s] })
 
def _assembleMetadata (request, stts, return_val):
    # There is no datacite_xml ezid profile. Just use 'datacite'
    return { "_profile": 'datacite',
      '_target' : uic.fix_target(request.POST['_target']),
      "_status": stts,
      "_export": ("yes" if 'export' in request.POST and\
                  request.POST['export']== "yes" or
                  request.POST['_export'] == 'yes' else "no"),
      "datacite": return_val }
