from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import override_settings

from wagtail.documents import (
    get_document_model,
    get_document_model_string,
    models,
    signal_handlers,
)
from wagtail.images.tests.utils import get_test_image_file
from wagtail.models import Collection, GroupCollectionPermission
from wagtail.test.testapp.models import CustomDocument, ReimportedDocumentModel
from wagtail.test.utils import WagtailTestUtils


class TestDocumentQuerySet(TransactionTestCase):
    fixtures = ["test_empty.json"]

    def test_search_method(self):
        # Make a test document
        document = models.Document.objects.create(title="Test document")

        # Search for it
        results = models.Document.objects.search("Test")
        self.assertEqual(list(results), [document])

    def test_operators(self):
        aaa_document = models.Document.objects.create(title="AAA Test document")
        zzz_document = models.Document.objects.create(title="ZZZ Test document")

        results = models.Document.objects.search("aaa test", operator="and")
        self.assertEqual(list(results), [aaa_document])

        results = models.Document.objects.search("aaa test", operator="or")
        sorted_results = sorted(results, key=lambda doc: doc.title)
        self.assertEqual(sorted_results, [aaa_document, zzz_document])

    def test_custom_ordering(self):
        aaa_document = models.Document.objects.create(title="AAA Test document")
        zzz_document = models.Document.objects.create(title="ZZZ Test document")

        results = models.Document.objects.order_by("title").search(
            "Test", order_by_relevance=False
        )
        self.assertEqual(list(results), [aaa_document, zzz_document])
        results = models.Document.objects.order_by("-title").search(
            "Test", order_by_relevance=False
        )
        self.assertEqual(list(results), [zzz_document, aaa_document])


class TestDocumentPermissions(WagtailTestUtils, TestCase):
    def setUp(self):
        # Create some user accounts for testing permissions
        self.user = self.create_user(
            username="user", email="user@email.com", password="password"
        )
        self.owner = self.create_user(
            username="owner", email="owner@email.com", password="password"
        )
        self.editor = self.create_user(
            username="editor", email="editor@email.com", password="password"
        )
        self.editor.groups.add(Group.objects.get(name="Editors"))
        self.administrator = self.create_superuser(
            username="administrator",
            email="administrator@email.com",
            password="password",
        )

        # Owner user must have the add_document permission
        self.adders_group = Group.objects.create(name="Document adders")
        GroupCollectionPermission.objects.create(
            group=self.adders_group,
            collection=Collection.get_first_root_node(),
            permission=Permission.objects.get(codename="add_document"),
        )
        self.owner.groups.add(self.adders_group)

        # Create a document for running tests on
        self.document = models.Document.objects.create(
            title="Test document", uploaded_by_user=self.owner
        )

    def test_administrator_can_edit(self):
        self.assertTrue(self.document.is_editable_by_user(self.administrator))

    def test_editor_can_edit(self):
        self.assertTrue(self.document.is_editable_by_user(self.editor))

    def test_owner_can_edit(self):
        self.assertTrue(self.document.is_editable_by_user(self.owner))

    def test_user_cant_edit(self):
        self.assertFalse(self.document.is_editable_by_user(self.user))


class TestDocumentFilenameProperties(TestCase):
    def setUp(self):
        self.document = models.Document(title="Test document")
        self.document.file.save(
            "sample_name.doc",
            ContentFile("A boring example document"),
        )

        self.pdf_document = models.Document(title="Test document")
        self.pdf_document.file.save(
            "sample_name.pdf",
            ContentFile("A boring example document"),
        )

        self.extensionless_document = models.Document(title="Test document")
        self.extensionless_document.file.save(
            "sample_name",
            ContentFile("A boring example document"),
        )

    def test_filename(self):
        self.assertEqual("sample_name.doc", self.document.filename)
        self.assertEqual("sample_name.pdf", self.pdf_document.filename)
        self.assertEqual("sample_name", self.extensionless_document.filename)

    def test_file_extension(self):
        self.assertEqual("doc", self.document.file_extension)
        self.assertEqual("pdf", self.pdf_document.file_extension)
        self.assertEqual("", self.extensionless_document.file_extension)

    def test_content_type(self):
        self.assertEqual("application/msword", self.document.content_type)
        self.assertEqual("application/pdf", self.pdf_document.content_type)
        self.assertEqual(
            "application/octet-stream", self.extensionless_document.content_type
        )

    def test_file_hash(self):
        self.assertEqual(
            self.document.get_file_hash(), "7d8c4778b182e4f3bd442408c64a6e22a4b0ed85"
        )
        self.assertEqual(
            self.pdf_document.get_file_hash(),
            "7d8c4778b182e4f3bd442408c64a6e22a4b0ed85",
        )
        self.assertEqual(
            self.extensionless_document.get_file_hash(),
            "7d8c4778b182e4f3bd442408c64a6e22a4b0ed85",
        )

    def test_content_disposition(self):
        self.assertEqual(
            """attachment; filename=sample_name.doc; filename*=UTF-8''sample_name.doc""",
            self.document.content_disposition,
        )
        self.assertEqual("inline", self.pdf_document.content_disposition)
        self.assertEqual(
            """attachment; filename=sample_name; filename*=UTF-8''sample_name""",
            self.extensionless_document.content_disposition,
        )

    def tearDown(self):
        # delete the FieldFile directly because the TestCase does not commit
        # transactions to trigger transaction.on_commit() in the signal handler
        self.document.file.delete()
        self.pdf_document.file.delete()
        self.extensionless_document.file.delete()


class TestFilesDeletedForDefaultModels(TransactionTestCase):
    """
    Because we expect file deletion to only happen once a transaction is
    successfully committed, we must run these tests using TransactionTestCase
    per the following documentation:

        Django's TestCase class wraps each test in a transaction and rolls back that
        transaction after each test, in order to provide test isolation. This means
        that no transaction is ever actually committed, thus your on_commit()
        callbacks will never be run. If you need to test the results of an
        on_commit() callback, use a TransactionTestCase instead.
        https://docs.djangoproject.com/en/1.10/topics/db/transactions/#use-in-tests
    """

    fixtures = ["test_empty.json"]

    def test_document_file_deleted_oncommit(self):
        with transaction.atomic():
            document = get_document_model().objects.create(
                title="Test Image", file=get_test_image_file()
            )
            filename = document.file.name

            self.assertTrue(document.file.storage.exists(filename))
            document.delete()
            self.assertTrue(document.file.storage.exists(filename))
        self.assertFalse(document.file.storage.exists(filename))


@override_settings(WAGTAILDOCS_EXTENSIONS=["pdf"])
class TestDocumentValidateExtensions(TestCase):
    def setUp(self):
        self.document_invalid = models.Document.objects.create(
            title="Test document", file="test.doc"
        )
        self.document_valid = models.Document.objects.create(
            title="Test document", file="test.pdf"
        )

    def test_create_doc_invalid_extension(self):
        """
        Checks if the uploaded document has the expected extensions
        mentioned in settings.WAGTAILDOCS_EXTENSIONS

        This is caught in form.error and should be raised during model
        creation when called full_clean. This specific testcase invalid
        file extension is passed
        """
        with self.assertRaises(ValidationError) as e:
            self.document_invalid.full_clean()
        expected_message = (
            "File extension “doc” is not allowed. Allowed extensions are: pdf."
        )
        self.assertEqual(e.exception.message_dict["file"][0], expected_message)

    def test_create_doc_valid_extension(self):
        """
        Checks if the uploaded document has the expected extensions
        mentioned in settings.WAGTAILDOCS_EXTENSIONS

        This is caught in form.error and should be raised during
        model creation when called full_clean. In this specific
        testcase invalid file extension is passed.
        """
        try:
            self.document_valid.full_clean()
        except ValidationError:
            self.fail("Validation error is raised even when valid file name is passed")

    def tearDown(self):
        self.document_invalid.file.delete()
        self.document_valid.file.delete()


@override_settings(WAGTAILDOCS_DOCUMENT_MODEL="tests.CustomDocument")
class TestFilesDeletedForCustomModels(TestFilesDeletedForDefaultModels):
    def setUp(self):
        # Required to create root collection because the TransactionTestCase
        # does not make initial data loaded in migrations available and
        # serialized_rollback=True causes other problems in the test suite.
        # ref: https://docs.djangoproject.com/en/1.10/topics/testing/overview/#rollback-emulation
        Collection.objects.get_or_create(
            name="Root",
            path="0001",
            depth=1,
            numchild=0,
        )

        #: Sadly signal receivers only get connected when starting django.
        #: We will re-attach them here to mimic the django startup behaviour
        #: and get the signals connected to our custom model..
        signal_handlers.register_signal_handlers()

    def test_document_model(self):
        cls = get_document_model()
        self.assertEqual(
            f"{cls._meta.app_label}.{cls.__name__}", "tests.CustomDocument"
        )


class TestGetDocumentModel(WagtailTestUtils, TestCase):
    @override_settings(WAGTAILDOCS_DOCUMENT_MODEL="tests.CustomDocument")
    def test_custom_get_document_model(self):
        """Test get_document_model with a custom document model"""
        self.assertIs(get_document_model(), CustomDocument)

    def test_get_document_model_at_import_time(self):
        self.assertEqual(ReimportedDocumentModel, models.Document)

    @override_settings(WAGTAILDOCS_DOCUMENT_MODEL="tests.CustomDocument")
    def test_custom_get_document_model_string(self):
        """Test get_document_model_string with a custom document model"""
        self.assertEqual(get_document_model_string(), "tests.CustomDocument")

    @override_settings()
    def test_standard_get_document_model(self):
        """Test get_document_model with no WAGTAILDOCS_DOCUMENT_MODEL"""
        del settings.WAGTAILDOCS_DOCUMENT_MODEL
        from wagtail.documents.models import Document

        self.assertIs(get_document_model(), Document)

    @override_settings()
    def test_standard_get_document_model_string(self):
        """Test get_document_model_string with no WAGTAILDOCS_DOCUMENT_MODEL"""
        del settings.WAGTAILDOCS_DOCUMENT_MODEL
        self.assertEqual(get_document_model_string(), "wagtaildocs.Document")

    @override_settings(WAGTAILDOCS_DOCUMENT_MODEL="tests.UnknownModel")
    def test_unknown_get_document_model(self):
        """Test get_document_model with an unknown model"""
        with self.assertRaises(ImproperlyConfigured):
            get_document_model()

    @override_settings(WAGTAILDOCS_DOCUMENT_MODEL="invalid-string")
    def test_invalid_get_document_model(self):
        """Test get_document_model with an invalid model string"""
        with self.assertRaises(ImproperlyConfigured):
            get_document_model()
