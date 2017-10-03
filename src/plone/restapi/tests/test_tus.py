# -*- coding: utf-8 -*-
from DateTime import DateTime
from StringIO import StringIO
from base64 import b64encode
from plone import api
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import SITE_OWNER_PASSWORD
from plone.app.testing import TEST_USER_ID
from plone.app.testing import TEST_USER_NAME
from plone.app.testing import TEST_USER_PASSWORD
from plone.app.testing import login
from plone.app.testing import setRoles
from plone.rest.cors import CORSPolicy
from plone.rest.interfaces import ICORSPolicy
from plone.restapi.services.content.tus import TUSUpload
from plone.restapi.testing import PLONE_RESTAPI_AT_FUNCTIONAL_TESTING
from plone.restapi.testing import PLONE_RESTAPI_DX_FUNCTIONAL_TESTING
from plone.restapi.testing import RelativeSession
from zope.component import getGlobalSiteManager
from zope.component import provideAdapter
from zope.interface import Interface
from zope.publisher.interfaces.browser import IBrowserRequest

import os
import shutil
import tempfile
import transaction
import unittest

UPLOAD_DATA = 'abcdefgh'
UPLOAD_MIMETYPE = 'text/plain'
UPLOAD_FILENAME = 'test.txt'
UPLOAD_LENGTH = len(UPLOAD_DATA)

UPLOAD_PDF_MIMETYPE = 'application/pdf'
UPLOAD_PDF_FILENAME = 'file.pdf'


class TestTUS(unittest.TestCase):

    layer = PLONE_RESTAPI_DX_FUNCTIONAL_TESTING

    def setUp(self):
        self.app = self.layer['app']
        self.portal = self.layer['portal']
        login(self.portal, SITE_OWNER_NAME)

        self.folder = api.content.create(container=self.portal,
                                         type='Folder',
                                         id='testfolder',
                                         title='Testfolder')
        self.upload_url = '{}/@tus-upload'.format(self.folder.absolute_url())
        transaction.commit()

        self.api_session = RelativeSession(self.portal.absolute_url())
        self.api_session.headers.update({'Accept': 'application/json'})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)

    def get_tus_uid_from_url(self, url):
        uid = url.rsplit('/', 1)[-1]
        assert len(uid) == 32
        return uid

    def get_tus_upload_instance(self, uid):
        return TUSUpload(uid)

    def test_tus_option_headers(self):
        response = self.api_session.options(self.upload_url)
        headers = response.headers
        self.assertEqual(response.status_code, 204)
        self.assertEqual(headers['Tus-Version'], '1.0.0')
        self.assertEqual(headers['Tus-Extension'], 'creation,expiration')
        self.assertEqual(headers['Tus-Resumable'], '1.0.0')

    def test_tus_post_without_version_header_returns_412(self):
        response = self.api_session.post(self.upload_url)
        self.assertEqual(412, response.status_code)

    def test_tus_post_with_wrong_version_header_returns_412(self):
        response = self.api_session.post(
            self.upload_url, headers={'Tus-Resumable': '0.2.2'})
        self.assertEqual(412, response.status_code)

    def test_tus_post_initialization_requires_header_length(self):
        response = self.api_session.post(
            self.upload_url, headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(response.json()['error']['type'], 'Bad Request')
        self.assertEqual(response.json()['error']['message'],
                         'Missing or invalid Upload-Length header')
        self.assertEqual(response.status_code, 400)

    def test_tus_post_initialization(self):
        response = self.api_session.post(
            self.upload_url,
            headers={
                'Tus-Resumable': '1.0.0',
                'Upload-Length': str(UPLOAD_LENGTH),
            })
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']
        url_base, uid = location.rsplit('/', 1)
        self.assertEqual(url_base, self.upload_url)
        self.assertEqual(len(uid), 32)
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(stored_metadata,
                         {u'length': 8, u'mode': u'create'})

    def test_tus_post_initialization_with_metadata(self):
        metadata = 'filename {},content-type {}'.format(
            b64encode(UPLOAD_FILENAME),
            b64encode(UPLOAD_MIMETYPE))
        response = self.api_session.post(
            self.upload_url,
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Length': str(UPLOAD_LENGTH),
                     'Upload-Metadata': metadata}
        )
        self.assertEqual(response.status_code, 201)
        uid = self.get_tus_uid_from_url(response.headers['Location'])
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(stored_metadata,
                         {u'content-type': u'text/plain',
                          u'filename': u'test.txt',
                          u'length': 8,
                          u'mode': u'create'})

    def test_tus_post_replace(self):
        self.file = api.content.create(container=self.portal,
                                       type='File',
                                       id='testfile',
                                       title='Testfile')
        transaction.commit()
        response = self.api_session.post(
            '{}/@tus-replace'.format(self.file.absolute_url()),
            headers={
                'Tus-Resumable': '1.0.0',
                'Upload-Length': str(UPLOAD_LENGTH),
            })
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']
        url_base, uid = location.rsplit('/', 1)
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(stored_metadata,
                         {u'length': 8, u'mode': u'replace'})

    def test_tus_head_on_not_existing_resource_returns_404(self):
        response = self.api_session.head(
            self.upload_url + '/myuid/123', headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)
        response = self.api_session.head(
            self.upload_url + '/myuid', headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)
        response = self.api_session.head(
            self.upload_url, headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)

    def test_tus_head_with_unsupported_version_returns_412(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.head(
            self.upload_url + '/myuid', headers={'Tus-Resumable': '0.2.2'})
        self.assertEqual(412, response.status_code)
        tus.cleanup()

    def test_tus_head_response_includes_required_headers(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.head(
            self.upload_url + '/myuid', headers={'Tus-Resumable': '1.0.0'})
        self.assertIn('Upload-Length', response.headers)
        self.assertEqual('2048', response.headers['Upload-Length'])
        self.assertIn('Upload-Offset', response.headers)
        self.assertIn('Tus-Resumable', response.headers)
        self.assertIn('Cache-Control', response.headers)
        tus.cleanup()

    def test_head_in_create_mode_without_add_permission_raises_401(self):
        self.folder.manage_permission('Add portal content', [], 0)
        transaction.commit()
        TUSUpload('myuid', {'mode': 'create', 'length': 12})
        response = self.api_session.head(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Offset': '0'})
        self.assertEqual(401, response.status_code)

    def test_head_in_replace_mode_without_modify_permission_raises_401(self):
        self.folder.manage_permission('Modify portal content', [], 0)
        transaction.commit()
        TUSUpload('myuid', {'mode': 'replace', 'length': 12})
        response = self.api_session.head(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Offset': '0'})
        self.assertEqual(401, response.status_code)

    def test_tus_patch_on_not_existing_resource_returns_404(self):
        response = self.api_session.patch(
            self.upload_url + '/myuid/123', headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)
        response = self.api_session.patch(
            self.upload_url + '/myuid', headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)
        response = self.api_session.patch(
            self.upload_url, headers={'Tus-Resumable': '1.0.0'})
        self.assertEqual(404, response.status_code)

    def test_tus_patch_with_unsupported_version_returns_412(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.patch(
            self.upload_url + '/myuid', headers={'Tus-Resumable': '0.2.2'})
        self.assertEqual(412, response.status_code)
        tus.cleanup()

    def test_tus_patch_with_unsupported_content_type_returns_400(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/json'})
        self.assertEqual(400, response.status_code)
        tus.cleanup()

    def test_tus_patch_with_invalid_offset_returns_400(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/offset+octet-stream'})
        self.assertEqual(400, response.status_code)
        tus.cleanup()

    def test_tus_patch_unfinished_upload_returns_expires_header(self):
        tus = TUSUpload('myuid', {'length': 2048})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0'},
            data=StringIO('abcdefghijkl'))
        self.assertEqual(204, response.status_code)
        self.assertIn('Upload-Expires', response.headers)
        tus.cleanup()

    def test_tus_patch_non_primary_field(self):
        tus = TUSUpload('myuid', {
            '@type': 'DXTestDocument',
            'length': 12,
            'fieldname': 'test_namedblobfile_field'})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0'},
            data=StringIO('abcdefghijkl'))

        self.assertEqual(204, response.status_code)
        transaction.commit()
        self.assertEqual(1, len(self.folder.objectIds()))
        id_ = self.folder.objectIds()[0]
        self.assertEqual(
            'abcdefghijkl', self.folder[id_].test_namedblobfile_field.data)
        tus.cleanup()

    def test_patch_in_create_mode_without_add_permission_raises_401(self):
        self.folder.manage_permission('Add portal content', [], 0)
        transaction.commit()
        TUSUpload('myuid', {'mode': 'create', 'length': 12})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0'},
            data=StringIO('abcdefghijkl'))
        self.assertEqual(401, response.status_code)

    def test_patch_in_replace_mode_without_modify_permission_raises_401(self):
        self.folder.manage_permission('Modify portal content', [], 0)
        transaction.commit()
        TUSUpload('myuid', {'mode': 'replace', 'length': 12})
        response = self.api_session.patch(
            self.upload_url + '/myuid',
            headers={'Tus-Resumable': '1.0.0',
                     'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0'},
            data=StringIO('abcdefghijkl'))
        self.assertEqual(401, response.status_code)

    def test_tus_can_upload_pdf_file(self):
        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__),
                                     UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = 'filename {},content-type {}'.format(
            b64encode(UPLOAD_PDF_FILENAME),
            b64encode(UPLOAD_PDF_MIMETYPE))
        response = self.api_session.post(
            self.upload_url,
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Length': str(pdf_file_size),
                     'Upload-Metadata': metadata}
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']

        # upload the data with PATCH
        pdf_file = open(pdf_file_path, 'rb')
        response = self.api_session.patch(
            location,
            headers={'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0',
                     'Tus-Resumable': '1.0.0'},
            data=pdf_file)
        self.assertEqual(response.status_code, 204)

        transaction.commit()
        self.assertEqual([UPLOAD_PDF_FILENAME], self.folder.contentIds())

    def test_tus_can_upload_text_file(self):
        # initialize the upload with POST
        metadata = 'filename {},content-type {}'.format(
            b64encode(UPLOAD_FILENAME),
            b64encode(UPLOAD_MIMETYPE))
        response = self.api_session.post(
            self.upload_url,
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Length': str(UPLOAD_LENGTH),
                     'Upload-Metadata': metadata}
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']

        # upload the data with PATCH
        response = self.api_session.patch(
            location,
            headers={'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0',
                     'Tus-Resumable': '1.0.0'},
            data=StringIO(UPLOAD_DATA))
        self.assertEqual(response.status_code, 204)

    def test_tus_can_replace_pdf_file(self):
        # Create a test file
        self.file = api.content.create(container=self.portal,
                                       type='File',
                                       id='testfile',
                                       title='Testfile')
        transaction.commit()
        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__),
                                     UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = 'filename {},content-type {}'.format(
            b64encode(UPLOAD_PDF_FILENAME),
            b64encode(UPLOAD_PDF_MIMETYPE))
        response = self.api_session.post(
            '{}/@tus-replace'.format(self.file.absolute_url()),
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Length': str(pdf_file_size),
                     'Upload-Metadata': metadata}
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']

        # upload the data with PATCH
        pdf_file = open(pdf_file_path, 'rb')
        response = self.api_session.patch(
            location,
            headers={'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0',
                     'Tus-Resumable': '1.0.0'},
            data=pdf_file)
        self.assertEqual(response.status_code, 204)

        transaction.commit()
        self.assertEqual(UPLOAD_PDF_FILENAME, self.file.file.filename)
        self.assertEqual(pdf_file_size, self.file.file.size)

    def tearDown(self):
        client_home = os.environ.get('CLIENT_HOME')
        tmp_dir = os.path.join(client_home, 'tus-uploads')
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)


class CORSTestPolicy(CORSPolicy):
    allow_origin = ['*']
    allow_methods = ['DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT']
    allow_credentials = True
    allow_headers = [
        'Accept', 'Authorization', 'Origin', 'X-Requested-With',
        'Content-Type', 'Tus-Resumable', 'Upload-Length', 'Upload-Offset',
    ]
    expose_header = []
    max_age = 3600


class TestTUSUploadWithCORS(unittest.TestCase):

    layer = PLONE_RESTAPI_DX_FUNCTIONAL_TESTING

    def setUp(self):
        provideAdapter(
            CORSTestPolicy,
            adapts=(Interface, IBrowserRequest),
            provides=ICORSPolicy,
        )
        self.portal = self.layer['portal']
        self.api_session = RelativeSession(self.portal.absolute_url())
        self.api_session.headers.update({'Accept': 'application/json'})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)
        self.upload_url = '{}/@tus-upload'.format(self.portal.absolute_url())

    def test_cors_preflight_for_post_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url, headers={
                'Origin': 'http://myhost.net',
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers':
                    'Tus-Resumable,Upload-Length',
            })
        self.assertIn('Tus-Resumable', response.headers)
        self.assertIn('Tus-Version', response.headers)
        self.assertIn('Tus-Extension', response.headers)

    def test_cors_preflight_for_patch_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url, headers={
                'Origin': 'http://myhost.net',
                'Access-Control-Request-Method': 'PATCH',
                'Access-Control-Request-Headers':
                    'Content-Type,Tus-Resumable,Upload-Offset',
            })
        self.assertIn('Tus-Resumable', response.headers)
        self.assertIn('Tus-Version', response.headers)
        self.assertIn('Tus-Extension', response.headers)

    def test_cors_preflight_for_head_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url, headers={
                'Origin': 'http://myhost.net',
                'Access-Control-Request-Method': 'HEAD',
                'Access-Control-Request-Headers':
                    'Tus-Resumable',
            })
        self.assertIn('Tus-Resumable', response.headers)
        self.assertIn('Tus-Version', response.headers)
        self.assertIn('Tus-Extension', response.headers)

    def tearDown(self):
        gsm = getGlobalSiteManager()
        gsm.unregisterAdapter(
            CORSTestPolicy, (Interface, IBrowserRequest), ICORSPolicy)


class TestTUSUpload(unittest.TestCase):

    def test_tmp_dir_gets_created_in_client_home(self):
        tus = TUSUpload('myuid')
        self.assertTrue(os.path.isdir(tus.tmp_dir))

    def test_use_tus_tmp_dir_if_provided(self):
        tus_upload_dir = tempfile.mkdtemp()
        os.environ['TUS_TMP_FILE_DIR'] = tus_upload_dir
        tus = TUSUpload('myuid')
        self.assertEqual(tus_upload_dir, tus.tmp_dir)

    def test_metadata_gets_stored_if_provided(self):
        tus = TUSUpload('myuid', {'length': 1024, 'filename': 'test.pdf'})
        self.assertIn('filename', tus.metadata())
        self.assertEqual('test.pdf', tus.metadata()['filename'])

    def test_length_returns_total_length_if_set(self):
        tus = TUSUpload('myuid', {'length': 1024})
        self.assertEqual(1024, tus.length())

    def test_length_returns_zero_if_not_set(self):
        tus = TUSUpload('myuid')
        self.assertEqual(0, tus.length())

    def test_offset_returns_zero_if_file_doesnt_exist(self):
        tus = TUSUpload('myuid', {'length': 1024})
        self.assertEqual(0, tus.offset())

    def test_offset_returns_size_of_current_file(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.write(StringIO('0123456789'))
        self.assertEqual(10, tus.offset())

    def test_write_creates_new_file(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.write(StringIO('0123456789'))
        self.assertTrue(os.path.isfile(tus.filepath))

    def test_write_appends_to_file_at_given_offset(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.write(StringIO('0123456789'))
        tus.write(StringIO('abc'), 10)
        self.assertEqual(13, tus.offset())
        with open(tus.filepath, 'rb') as f:
            data = f.read()
        self.assertEqual('0123456789abc', data)

    def test_write_sets_finished_flag(self):
        tus = TUSUpload('myuid', {'length': 10})
        tus.write(StringIO('0123456789'))
        self.assertTrue(tus.finished)

    def test_metadata_returns_empty_dict_if_no_metadata_has_been_set(self):
        tus = TUSUpload('myuid')
        self.assertEqual({}, tus.metadata())

    def test_expires_returns_expiration_time_of_current_upload(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.write(StringIO('0123456789'))
        self.assertGreater(DateTime(tus.expires()), DateTime())

    def test_cleanup_removes_upload_file(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.write(StringIO('0123456789'))
        tus.cleanup()
        self.assertFalse(os.path.exists(tus.filepath))

    def test_cleanup_removes_metadata_file(self):
        tus = TUSUpload('myuid', {'length': 1024})
        tus.cleanup()
        self.assertFalse(os.path.exists(tus.metadata_path))

    def test_cleanup_expired_files(self):
        tus = TUSUpload('myuid')
        filepath = os.path.join(tus.tmp_dir, 'tus_upload_12345')
        metadata_path = os.path.join(tus.tmp_dir, 'tus_upload_12345.json')
        metadata_only_path = os.path.join(tus.tmp_dir, 'tus_upload_67890.json')
        open(filepath, 'wb').close()
        os.utime(filepath, (946684800.0, 946684800.0))
        open(metadata_path, 'wb').close()
        os.utime(metadata_path, (946684800.0, 946684800.0))
        open(metadata_only_path, 'wb').close()
        os.utime(metadata_only_path, (946684800.0, 946684800.0))
        tus.cleanup_expired()
        self.assertFalse(os.path.exists(filepath))
        self.assertFalse(os.path.exists(metadata_path))
        self.assertFalse(os.path.exists(metadata_only_path))

    def tearDown(self):
        client_home = os.environ.get('CLIENT_HOME')
        tmp_dir = os.path.join(client_home, 'tus-uploads')
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)


class TestTUSWithAT(unittest.TestCase):

    layer = PLONE_RESTAPI_AT_FUNCTIONAL_TESTING

    def setUp(self):
        self.portal = self.layer['portal']
        setRoles(self.portal, TEST_USER_ID, ['Manager'])
        login(self.portal, TEST_USER_NAME)

        self.folder = api.content.create(container=self.portal,
                                         type='Folder',
                                         id='testfolder',
                                         title='Testfolder')
        self.upload_url = '{}/@tus-upload'.format(self.folder.absolute_url())
        transaction.commit()

        self.api_session = RelativeSession(self.portal.absolute_url())
        self.api_session.headers.update({'Accept': 'application/json'})
        self.api_session.auth = (TEST_USER_NAME, TEST_USER_PASSWORD)

    def test_tus_can_upload_pdf_file(self):
        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__),
                                     UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = 'filename {},content-type {}'.format(
            b64encode(UPLOAD_PDF_FILENAME),
            b64encode(UPLOAD_PDF_MIMETYPE))
        response = self.api_session.post(
            self.upload_url,
            headers={'Tus-Resumable': '1.0.0',
                     'Upload-Length': str(pdf_file_size),
                     'Upload-Metadata': metadata}
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers['Location']

        # upload the data with PATCH
        pdf_file = open(pdf_file_path, 'rb')
        response = self.api_session.patch(
            location,
            headers={'Content-Type': 'application/offset+octet-stream',
                     'Upload-Offset': '0',
                     'Tus-Resumable': '1.0.0'},
            data=pdf_file)
        self.assertEqual(response.status_code, 204)

        transaction.commit()
        self.assertEqual([UPLOAD_PDF_FILENAME], self.folder.contentIds())
