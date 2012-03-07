import ui_common as uic
import django.contrib.messages
from django.shortcuts import redirect
from django.core.urlresolvers import reverse
import ezid
import metadata
import search

FIELD_ORDER = ['identifier', 'owner', 'coOwners', 'createTime', 'updateTime', 'status',\
                'mappedTitle', 'mappedCreator']

FIELDS_MAPPED = {'identifier':'ID',  'owner':'Owner', 'coOwners': 'Co-Owners', \
                  'createTime': 'Date created', 'updateTime': 'Date last modified', 'status' :'Status',\
                  'mappedTitle': 'Title', 'mappedCreator' : 'Creator'}

FIELD_DEFAULTS = ['identifier', 'createTime', 'mappedTitle', 'mappedCreator']

FIELD_WIDTHS = {'identifier': 1.0,  'owner': 1.0, 'coOwners': 2.0, \
                'createTime': 2.0, 'updateTime': 2.0, 'status' :1.0,\
                'mappedTitle': 3.0, 'mappedCreator' : 2.0}

FIELD_DISPLAY_TYPES = {'identifier': 'identifier',  'owner': 'string', 'coOwners': 'string', \
                'createTime': 'datetime', 'updateTime': 'datetime', 'status' :'string',\
                'mappedTitle': 'string', 'mappedCreator' : 'string'}

def index(request):
  d = { 'menu_item' : 'ui_manage.index' }
  d['user'] = request.session['auth'].user
  d['recent'] = search.getByOwner(d['user'][1], False, 'updateTime', False, 10, 0)
  d['recent1'] = d['recent'][0:5]
  d['recent2'] = d['recent'][5:10]
  d['field_order'] = FIELD_ORDER
  d['fields_mapped'] = FIELDS_MAPPED
  d['field_defaults'] = FIELD_DEFAULTS
  d['fields_selected'] = [x for x in FIELD_ORDER if x in request.REQUEST ]
  if len(d['fields_selected']) < 1: d['fields_selected'] = FIELD_DEFAULTS
  d['REQUEST'] = request.REQUEST
  d['field_widths'] = FIELD_WIDTHS
  d['field_display_types'] = FIELD_DISPLAY_TYPES
  return uic.render(request, 'manage/index', d)

def edit(request, identifier):
  d = { 'menu_item' : 'ui_manage.null'}
  r = ezid.getMetadata(identifier)
  if type(r) is str:
    django.contrib.messages.error(request, uic.formatError(r))
    return uic.redirect("ui_lookup.index")
  if not uic.authorizeUpdate(request, r):
    django.contrib.messages.error(request, "You are not allowed to edit this identifier")
    return redirect("ui_manage.details", identifier)
  s, m = r
  d['status'] = m['_status'] if '_status' in m else 'unavailable'
  d['post_status'] = d['status']
  d['id_text'] = s.split()[1]
  d['identifier'] = m # identifier object containing metadata
  d['internal_profile'] = metadata.getProfile('internal')
  if request.method == "POST":
    d['post_status'] = request.POST['_status'] if '_status' in request.POST else 'public'
    d['current_profile'] = metadata.getProfile(request.POST['current_profile'])
    if request.POST['current_profile'] == request.POST['original_profile']:
      #this means we're saving and going to a save confirmation page
      if uic.validate_simple_metadata_form(request, d['current_profile']):
        result = uic.write_profile_elements_from_form(identifier, request, d['current_profile'],
                 {'_profile': request.POST['current_profile'], '_target' : request.POST['_target'],
                  '_status': d['post_status']})
        if result:
          django.contrib.messages.success(request, "Identifier updated.")
          return redirect("ui_manage.details", identifier)
        else:
          django.contrib.messages.error(request, "There was an error updating the metadata for your identifier: " + s)
          return uic.render(request, "manage/edit", d)
  elif request.method == "GET":
    if '_profile' in m:
      d['current_profile'] = metadata.getProfile(m['_profile'])
    else:
      d['current_profile'] = metadata.getProfile('dc')
  d['profiles'] = metadata.getProfiles()[1:]
  return uic.render(request, "manage/edit", d)

def details(request, identifier):
  d = { 'menu_item' : 'ui_manage.null'}
  r = ezid.getMetadata(identifier)
  if type(r) is str:
    print r
    django.contrib.messages.error(request, uic.formatError(r))
    return redirect("ui_lookup.index")
  d['allow_update'] = uic.authorizeUpdate(request, r)
  s, m = r
  assert s.startswith("success:")
  if not uic.view_authorized_for_identifier(request, m): return redirect("ui_lookup.index")
  d['id_text'] = s.split()[1]
  d['identifier'] = m # identifier object containing metadata
  d['internal_profile'] = metadata.getProfile('internal')
  d['target'] = d['identifier']['_target']
  if '_profile' in m:
    d['current_profile'] = metadata.getProfile(m['_profile'])
  else:
    d['current_profile'] = metadata.getProfile('dc')
  return uic.render(request, "manage/details", d)