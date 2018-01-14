#!/usr/bin/env python
"""Form to Parse'erator"""
from __future__ import division

import base64
import datetime
import logging

from google.appengine.api import app_identity
from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.cloud import storage
import googleapiclient.discovery                 # noqa: I201

from flask import Flask                          # noqa: I100, I201
import requests                                  # noqa: I201
import requests_toolbelt.adapters.appengine      # noqa: I201
import yaml                                      # noqa: I201


DEBUG              = False                       # noqa: E221
TYPEFORM_FORM_ID   = '...'                       # noqa: E221
PARSE_CLASS        = '...'                       # noqa: E221
TYPEFORMAPI_URL    = 'https://api.typeform.com'  # noqa: E221
TYPEFORMAPI_PATH   = '/forms/{form_id}/responses'.format(form_id=TYPEFORM_FORM_ID)  # noqa: E221, E501
PARSEAPI_URL       = 'https://...'               # noqa: E221
PARSEAPI_PATH      = '/parse/classes/{class_name}'.format(class_name=PARSE_CLASS)   # noqa: E221, E501
KMS_BUCKET         = '...'                       # noqa: E221
KMS_LOCATION       = 'global'                    # noqa: E221
KMS_KEYRING        = '...'                       # noqa: E221
TYPEFORM_CRYPTOKEY = '...'                       # noqa: E221
TYPEFORM_API_FILE  = '....encrypted'             # noqa: E221
PARSE_CRYPTOKEY    = '...'                       # noqa: E221
PARSE_API_FILE     = '....encrypted'             # noqa: E221

TYPEFORM_HIDDEN_FIELDS = ['...']
TYPFEORM_NONHIDDEN_FIELDS = ['...']
TYPEFORM_PUSH_INCOMPLETE_HIDDEN_FIELDS = False


app = Flask(__name__)
requests_toolbelt.adapters.appengine.monkeypatch()
urlfetch.set_default_fetch_deadline(45)


def _decrypt(project_id, location, keyring, cryptokey, cipher_text):
    """Decrypts and returns string from given cipher text."""
    logging.info('Decrypting cryptokey: {}'.format(cryptokey))
    kms_client = googleapiclient.discovery.build('cloudkms', 'v1')
    name = 'projects/{}/locations/{}/keyRings/{}/cryptoKeys/{}'.format(
        project_id, location, keyring, cryptokey)
    cryptokeys = kms_client.projects().locations().keyRings().cryptoKeys()
    request = cryptokeys.decrypt(
        name=name,
        body={'ciphertext': base64.b64encode(cipher_text).decode('ascii')})
    response = request.execute()
    return base64.b64decode(response['plaintext'].encode('ascii'))


def _download_output(output_bucket, filename):
    """Downloads the output file from GCS and returns it as a string."""
    logging.info('Downloading output file')
    client = storage.Client()
    bucket = client.get_bucket(output_bucket)
    output_blob = (
        'keys/{}'
        .format(filename))
    return bucket.blob(output_blob).download_as_string()


def get_credentials(cryptokey, filename):
    """Fetches credentials from KMS returning a decrypted API key."""
    credentials_enc = _download_output(KMS_BUCKET, filename)
    credentials_dec = _decrypt(app_identity.get_application_id(),
                               KMS_LOCATION,
                               KMS_KEYRING,
                               cryptokey,
                               credentials_enc)
    credentials_dec_yaml = yaml.load(credentials_dec)
    return credentials_dec_yaml


def push_to_parse(parse_creds, entry):
    """Makes individual requests to Parse."""
    logging.info('Pushing entry to Parse')
    header_dict = {'X-Parse-Application-Id': parse_creds['app_id'],
                   'X-Parse-REST-API-Key': parse_creds['rest_key'],
                   'X-Parse-Master-Key': parse_creds['master_key'],
                   }
    params_dict = {}

    if ('answers' in entry) and (entry['answers'] is not None):
        for answer in entry['answers']:
            for field in TYPFEORM_NONHIDDEN_FIELDS:
                if answer['type'] == field:
                    params_dict[field] = answer[field]
    else:
        logging.info('No answers recorded in entry')

        if DEBUG:
            logging.info('Entry for debugging: {entry}'.format(entry=entry))  # noqa: E501

    # Record any hidden fields
    for hidden_field in TYPEFORM_HIDDEN_FIELDS:
        if hidden_field in entry['hidden']:
            params_dict[hidden_field] = entry['hidden'][hidden_field]
        else:
            logging.info('Expected hidden field that was not present')

            if DEBUG:
                logging.info('Entry for debugging: {entry}'.format(entry=entry))  # noqa: E501

            if not TYPEFORM_PUSH_INCOMPLETE_HIDDEN_FIELDS:
                logging.info('Not pushing incomplete entry with missing hidden field')  # noqa: E501
                return

    url = PARSEAPI_URL + PARSEAPI_PATH

    try:
        r = requests.post(url, data=params_dict, headers=header_dict)
        r.json()
    except Exception as error:
        logging.error('An error occurred pushing data to parse: {0}'.format(error))  # noqa: E501
        raise error


def fetch_typeform(typeform_creds, last_successful_runtime):
    """Fetches data from Typeform."""
    logging.info('Fetching data from Typeform')

    header_dict = {'Authorization': 'Bearer {token}'.format(token=typeform_creds['typeform_api_key']),  # noqa: E501
                  }
    params = {}

    if last_successful_runtime:
        params['since'] = last_successful_runtime

    url = TYPEFORMAPI_URL + TYPEFORMAPI_PATH

    try:
        r = requests.get(url, params=params, headers=header_dict)
        logging.info('URL: {0}'.format(r.url))
        response = r.json()
    except Exception as error:
        logging.error('An error occurred fetching data from typeform: {0}'.format(error))  # noqa: E501
        response = None
        raise error

    return response


def get_last_successful_runtime():
    last_runtime = memcache.get(key='last_successful_runtime')

    logging.info('Last successful runtime: {0}'.format(last_runtime))

    return last_runtime


def update_successful_runtime():
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat()  # noqa: E501

    if memcache.get(key='last_successful_runtime'):
        memcache.replace(key='last_successful_runtime', value=now)
    else:
        memcache.add(key='last_successful_runtime', value=now)


@app.route('/run')
def run():
    """Runs the task."""
    typeform_creds = get_credentials(TYPEFORM_CRYPTOKEY, TYPEFORM_API_FILE)
    parse_creds    = get_credentials(PARSE_CRYPTOKEY, PARSE_API_FILE)  # noqa: E221, E501

    new_entries = fetch_typeform(typeform_creds, get_last_successful_runtime())

    if 'items' in new_entries:
        logging.info('There are {0} new entries to push to Parse'.format(len(new_entries['items'])))  # noqa: E501

        for entry in new_entries['items']:
            push_to_parse(parse_creds, entry)
    else:
        logging.error('Unexpected: No returned "items" in returned data from Typeform: {0}'.format(new_entries))  # noqa: E501

    update_successful_runtime()

    return 'Completed', 200


@app.errorhandler(500)
def server_error(e):
    logging.exception('An error occurred during a request: {0}'.format(e))
    return 'An internal error occurred.', 500
