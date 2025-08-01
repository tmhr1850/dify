import json

from flask import request
from flask_restful import marshal, reqparse
from sqlalchemy import desc, select
from werkzeug.exceptions import Forbidden, NotFound

import services
from controllers.common.errors import FilenameNotExistsError
from controllers.service_api import api
from controllers.service_api.app.error import (
    FileTooLargeError,
    NoFileUploadedError,
    ProviderNotInitializeError,
    TooManyFilesError,
    UnsupportedFileTypeError,
)
from controllers.service_api.dataset.error import (
    ArchivedDocumentImmutableError,
    DocumentIndexingError,
    InvalidMetadataError,
)
from controllers.service_api.wraps import (
    DatasetApiResource,
    cloud_edition_billing_rate_limit_check,
    cloud_edition_billing_resource_check,
)
from core.errors.error import ProviderTokenNotInitError
from extensions.ext_database import db
from fields.document_fields import document_fields, document_status_fields
from libs.login import current_user
from models.dataset import Dataset, Document, DocumentSegment
from services.dataset_service import DatasetService, DocumentService
from services.entities.knowledge_entities.knowledge_entities import KnowledgeConfig
from services.file_service import FileService


class DocumentAddByTextApi(DatasetApiResource):
    """Resource for documents."""

    @cloud_edition_billing_resource_check("vector_space", "dataset")
    @cloud_edition_billing_resource_check("documents", "dataset")
    @cloud_edition_billing_rate_limit_check("knowledge", "dataset")
    def post(self, tenant_id, dataset_id):
        """Create document by text."""
        parser = reqparse.RequestParser()
        parser.add_argument("name", type=str, required=True, nullable=False, location="json")
        parser.add_argument("text", type=str, required=True, nullable=False, location="json")
        parser.add_argument("process_rule", type=dict, required=False, nullable=True, location="json")
        parser.add_argument("original_document_id", type=str, required=False, location="json")
        parser.add_argument("doc_form", type=str, default="text_model", required=False, nullable=False, location="json")
        parser.add_argument(
            "doc_language", type=str, default="English", required=False, nullable=False, location="json"
        )
        parser.add_argument(
            "indexing_technique", type=str, choices=Dataset.INDEXING_TECHNIQUE_LIST, nullable=False, location="json"
        )
        parser.add_argument("retrieval_model", type=dict, required=False, nullable=True, location="json")
        parser.add_argument("embedding_model", type=str, required=False, nullable=True, location="json")
        parser.add_argument("embedding_model_provider", type=str, required=False, nullable=True, location="json")

        args = parser.parse_args()

        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()

        if not dataset:
            raise ValueError("Dataset does not exist.")

        if not dataset.indexing_technique and not args["indexing_technique"]:
            raise ValueError("indexing_technique is required.")

        text = args.get("text")
        name = args.get("name")
        if text is None or name is None:
            raise ValueError("Both 'text' and 'name' must be non-null values.")

        if args.get("embedding_model_provider"):
            DatasetService.check_embedding_model_setting(
                tenant_id, args.get("embedding_model_provider"), args.get("embedding_model")
            )
        if (
            args.get("retrieval_model")
            and args.get("retrieval_model").get("reranking_model")
            and args.get("retrieval_model").get("reranking_model").get("reranking_provider_name")
        ):
            DatasetService.check_reranking_model_setting(
                tenant_id,
                args.get("retrieval_model").get("reranking_model").get("reranking_provider_name"),
                args.get("retrieval_model").get("reranking_model").get("reranking_model_name"),
            )

        upload_file = FileService.upload_text(text=str(text), text_name=str(name))
        data_source = {
            "type": "upload_file",
            "info_list": {"data_source_type": "upload_file", "file_info_list": {"file_ids": [upload_file.id]}},
        }
        args["data_source"] = data_source
        knowledge_config = KnowledgeConfig(**args)
        # validate args
        DocumentService.document_create_args_validate(knowledge_config)

        try:
            documents, batch = DocumentService.save_document_with_dataset_id(
                dataset=dataset,
                knowledge_config=knowledge_config,
                account=current_user,
                dataset_process_rule=dataset.latest_process_rule if "process_rule" not in args else None,
                created_from="api",
            )
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        document = documents[0]

        documents_and_batch_fields = {"document": marshal(document, document_fields), "batch": batch}
        return documents_and_batch_fields, 200


class DocumentUpdateByTextApi(DatasetApiResource):
    """Resource for update documents."""

    @cloud_edition_billing_resource_check("vector_space", "dataset")
    @cloud_edition_billing_rate_limit_check("knowledge", "dataset")
    def post(self, tenant_id, dataset_id, document_id):
        """Update document by text."""
        parser = reqparse.RequestParser()
        parser.add_argument("name", type=str, required=False, nullable=True, location="json")
        parser.add_argument("text", type=str, required=False, nullable=True, location="json")
        parser.add_argument("process_rule", type=dict, required=False, nullable=True, location="json")
        parser.add_argument("doc_form", type=str, default="text_model", required=False, nullable=False, location="json")
        parser.add_argument(
            "doc_language", type=str, default="English", required=False, nullable=False, location="json"
        )
        parser.add_argument("retrieval_model", type=dict, required=False, nullable=False, location="json")
        args = parser.parse_args()
        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()

        if not dataset:
            raise ValueError("Dataset does not exist.")

        if (
            args.get("retrieval_model")
            and args.get("retrieval_model").get("reranking_model")
            and args.get("retrieval_model").get("reranking_model").get("reranking_provider_name")
        ):
            DatasetService.check_reranking_model_setting(
                tenant_id,
                args.get("retrieval_model").get("reranking_model").get("reranking_provider_name"),
                args.get("retrieval_model").get("reranking_model").get("reranking_model_name"),
            )

        # indexing_technique is already set in dataset since this is an update
        args["indexing_technique"] = dataset.indexing_technique

        if args["text"]:
            text = args.get("text")
            name = args.get("name")
            if text is None or name is None:
                raise ValueError("Both text and name must be strings.")
            upload_file = FileService.upload_text(text=str(text), text_name=str(name))
            data_source = {
                "type": "upload_file",
                "info_list": {"data_source_type": "upload_file", "file_info_list": {"file_ids": [upload_file.id]}},
            }
            args["data_source"] = data_source
        # validate args
        args["original_document_id"] = str(document_id)
        knowledge_config = KnowledgeConfig(**args)
        DocumentService.document_create_args_validate(knowledge_config)

        try:
            documents, batch = DocumentService.save_document_with_dataset_id(
                dataset=dataset,
                knowledge_config=knowledge_config,
                account=current_user,
                dataset_process_rule=dataset.latest_process_rule if "process_rule" not in args else None,
                created_from="api",
            )
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        document = documents[0]

        documents_and_batch_fields = {"document": marshal(document, document_fields), "batch": batch}
        return documents_and_batch_fields, 200


class DocumentAddByFileApi(DatasetApiResource):
    """Resource for documents."""

    @cloud_edition_billing_resource_check("vector_space", "dataset")
    @cloud_edition_billing_resource_check("documents", "dataset")
    @cloud_edition_billing_rate_limit_check("knowledge", "dataset")
    def post(self, tenant_id, dataset_id):
        """Create document by upload file."""
        args = {}
        if "data" in request.form:
            args = json.loads(request.form["data"])
        if "doc_form" not in args:
            args["doc_form"] = "text_model"
        if "doc_language" not in args:
            args["doc_language"] = "English"

        # get dataset info
        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()

        if not dataset:
            raise ValueError("Dataset does not exist.")

        if dataset.provider == "external":
            raise ValueError("External datasets are not supported.")

        indexing_technique = args.get("indexing_technique") or dataset.indexing_technique
        if not indexing_technique:
            raise ValueError("indexing_technique is required.")
        args["indexing_technique"] = indexing_technique

        if "embedding_model_provider" in args:
            DatasetService.check_embedding_model_setting(
                tenant_id, args["embedding_model_provider"], args["embedding_model"]
            )
        if (
            "retrieval_model" in args
            and args["retrieval_model"].get("reranking_model")
            and args["retrieval_model"].get("reranking_model").get("reranking_provider_name")
        ):
            DatasetService.check_reranking_model_setting(
                tenant_id,
                args["retrieval_model"].get("reranking_model").get("reranking_provider_name"),
                args["retrieval_model"].get("reranking_model").get("reranking_model_name"),
            )

        # save file info
        file = request.files["file"]
        # check file
        if "file" not in request.files:
            raise NoFileUploadedError()

        if len(request.files) > 1:
            raise TooManyFilesError()

        if not file.filename:
            raise FilenameNotExistsError

        upload_file = FileService.upload_file(
            filename=file.filename,
            content=file.read(),
            mimetype=file.mimetype,
            user=current_user,
            source="datasets",
        )
        data_source = {
            "type": "upload_file",
            "info_list": {"data_source_type": "upload_file", "file_info_list": {"file_ids": [upload_file.id]}},
        }
        args["data_source"] = data_source
        # validate args
        knowledge_config = KnowledgeConfig(**args)
        DocumentService.document_create_args_validate(knowledge_config)

        dataset_process_rule = dataset.latest_process_rule if "process_rule" not in args else None
        if not knowledge_config.original_document_id and not dataset_process_rule and not knowledge_config.process_rule:
            raise ValueError("process_rule is required.")

        try:
            documents, batch = DocumentService.save_document_with_dataset_id(
                dataset=dataset,
                knowledge_config=knowledge_config,
                account=dataset.created_by_account,
                dataset_process_rule=dataset_process_rule,
                created_from="api",
            )
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        document = documents[0]
        documents_and_batch_fields = {"document": marshal(document, document_fields), "batch": batch}
        return documents_and_batch_fields, 200


class DocumentUpdateByFileApi(DatasetApiResource):
    """Resource for update documents."""

    @cloud_edition_billing_resource_check("vector_space", "dataset")
    @cloud_edition_billing_rate_limit_check("knowledge", "dataset")
    def post(self, tenant_id, dataset_id, document_id):
        """Update document by upload file."""
        args = {}
        if "data" in request.form:
            args = json.loads(request.form["data"])
        if "doc_form" not in args:
            args["doc_form"] = "text_model"
        if "doc_language" not in args:
            args["doc_language"] = "English"

        # get dataset info
        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()

        if not dataset:
            raise ValueError("Dataset does not exist.")

        if dataset.provider == "external":
            raise ValueError("External datasets are not supported.")

        # indexing_technique is already set in dataset since this is an update
        args["indexing_technique"] = dataset.indexing_technique

        if "file" in request.files:
            # save file info
            file = request.files["file"]

            if len(request.files) > 1:
                raise TooManyFilesError()

            if not file.filename:
                raise FilenameNotExistsError

            try:
                upload_file = FileService.upload_file(
                    filename=file.filename,
                    content=file.read(),
                    mimetype=file.mimetype,
                    user=current_user,
                    source="datasets",
                )
            except services.errors.file.FileTooLargeError as file_too_large_error:
                raise FileTooLargeError(file_too_large_error.description)
            except services.errors.file.UnsupportedFileTypeError:
                raise UnsupportedFileTypeError()
            data_source = {
                "type": "upload_file",
                "info_list": {"data_source_type": "upload_file", "file_info_list": {"file_ids": [upload_file.id]}},
            }
            args["data_source"] = data_source
        # validate args
        args["original_document_id"] = str(document_id)

        knowledge_config = KnowledgeConfig(**args)
        DocumentService.document_create_args_validate(knowledge_config)

        try:
            documents, batch = DocumentService.save_document_with_dataset_id(
                dataset=dataset,
                knowledge_config=knowledge_config,
                account=dataset.created_by_account,
                dataset_process_rule=dataset.latest_process_rule if "process_rule" not in args else None,
                created_from="api",
            )
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        document = documents[0]
        documents_and_batch_fields = {"document": marshal(document, document_fields), "batch": document.batch}
        return documents_and_batch_fields, 200


class DocumentDeleteApi(DatasetApiResource):
    @cloud_edition_billing_rate_limit_check("knowledge", "dataset")
    def delete(self, tenant_id, dataset_id, document_id):
        """Delete document."""
        document_id = str(document_id)
        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)

        # get dataset info
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()

        if not dataset:
            raise ValueError("Dataset does not exist.")

        document = DocumentService.get_document(dataset.id, document_id)

        # 404 if document not found
        if document is None:
            raise NotFound("Document Not Exists.")

        # 403 if document is archived
        if DocumentService.check_archived(document):
            raise ArchivedDocumentImmutableError()

        try:
            # delete document
            DocumentService.delete_document(document)
        except services.errors.document.DocumentIndexingError:
            raise DocumentIndexingError("Cannot delete document during indexing.")

        return 204


class DocumentListApi(DatasetApiResource):
    def get(self, tenant_id, dataset_id):
        dataset_id = str(dataset_id)
        tenant_id = str(tenant_id)
        page = request.args.get("page", default=1, type=int)
        limit = request.args.get("limit", default=20, type=int)
        search = request.args.get("keyword", default=None, type=str)
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()
        if not dataset:
            raise NotFound("Dataset not found.")

        query = select(Document).filter_by(dataset_id=str(dataset_id), tenant_id=tenant_id)

        if search:
            search = f"%{search}%"
            query = query.where(Document.name.like(search))

        query = query.order_by(desc(Document.created_at), desc(Document.position))

        paginated_documents = db.paginate(select=query, page=page, per_page=limit, max_per_page=100, error_out=False)
        documents = paginated_documents.items

        response = {
            "data": marshal(documents, document_fields),
            "has_more": len(documents) == limit,
            "limit": limit,
            "total": paginated_documents.total,
            "page": page,
        }

        return response


class DocumentIndexingStatusApi(DatasetApiResource):
    def get(self, tenant_id, dataset_id, batch):
        dataset_id = str(dataset_id)
        batch = str(batch)
        tenant_id = str(tenant_id)
        # get dataset
        dataset = db.session.query(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id).first()
        if not dataset:
            raise NotFound("Dataset not found.")
        # get documents
        documents = DocumentService.get_batch_documents(dataset_id, batch)
        if not documents:
            raise NotFound("Documents not found.")
        documents_status = []
        for document in documents:
            completed_segments = (
                db.session.query(DocumentSegment)
                .where(
                    DocumentSegment.completed_at.isnot(None),
                    DocumentSegment.document_id == str(document.id),
                    DocumentSegment.status != "re_segment",
                )
                .count()
            )
            total_segments = (
                db.session.query(DocumentSegment)
                .where(DocumentSegment.document_id == str(document.id), DocumentSegment.status != "re_segment")
                .count()
            )
            # Create a dictionary with document attributes and additional fields
            document_dict = {
                "id": document.id,
                "indexing_status": "paused" if document.is_paused else document.indexing_status,
                "processing_started_at": document.processing_started_at,
                "parsing_completed_at": document.parsing_completed_at,
                "cleaning_completed_at": document.cleaning_completed_at,
                "splitting_completed_at": document.splitting_completed_at,
                "completed_at": document.completed_at,
                "paused_at": document.paused_at,
                "error": document.error,
                "stopped_at": document.stopped_at,
                "completed_segments": completed_segments,
                "total_segments": total_segments,
            }
            documents_status.append(marshal(document_dict, document_status_fields))
        data = {"data": documents_status}
        return data


class DocumentDetailApi(DatasetApiResource):
    METADATA_CHOICES = {"all", "only", "without"}

    def get(self, tenant_id, dataset_id, document_id):
        dataset_id = str(dataset_id)
        document_id = str(document_id)

        dataset = self.get_dataset(dataset_id, tenant_id)

        document = DocumentService.get_document(dataset.id, document_id)

        if not document:
            raise NotFound("Document not found.")

        if document.tenant_id != str(tenant_id):
            raise Forbidden("No permission.")

        metadata = request.args.get("metadata", "all")
        if metadata not in self.METADATA_CHOICES:
            raise InvalidMetadataError(f"Invalid metadata value: {metadata}")

        if metadata == "only":
            response = {"id": document.id, "doc_type": document.doc_type, "doc_metadata": document.doc_metadata_details}
        elif metadata == "without":
            dataset_process_rules = DatasetService.get_process_rules(dataset_id)
            document_process_rules = document.dataset_process_rule.to_dict()
            data_source_info = document.data_source_detail_dict
            response = {
                "id": document.id,
                "position": document.position,
                "data_source_type": document.data_source_type,
                "data_source_info": data_source_info,
                "dataset_process_rule_id": document.dataset_process_rule_id,
                "dataset_process_rule": dataset_process_rules,
                "document_process_rule": document_process_rules,
                "name": document.name,
                "created_from": document.created_from,
                "created_by": document.created_by,
                "created_at": document.created_at.timestamp(),
                "tokens": document.tokens,
                "indexing_status": document.indexing_status,
                "completed_at": int(document.completed_at.timestamp()) if document.completed_at else None,
                "updated_at": int(document.updated_at.timestamp()) if document.updated_at else None,
                "indexing_latency": document.indexing_latency,
                "error": document.error,
                "enabled": document.enabled,
                "disabled_at": int(document.disabled_at.timestamp()) if document.disabled_at else None,
                "disabled_by": document.disabled_by,
                "archived": document.archived,
                "segment_count": document.segment_count,
                "average_segment_length": document.average_segment_length,
                "hit_count": document.hit_count,
                "display_status": document.display_status,
                "doc_form": document.doc_form,
                "doc_language": document.doc_language,
            }
        else:
            dataset_process_rules = DatasetService.get_process_rules(dataset_id)
            document_process_rules = document.dataset_process_rule.to_dict()
            data_source_info = document.data_source_detail_dict
            response = {
                "id": document.id,
                "position": document.position,
                "data_source_type": document.data_source_type,
                "data_source_info": data_source_info,
                "dataset_process_rule_id": document.dataset_process_rule_id,
                "dataset_process_rule": dataset_process_rules,
                "document_process_rule": document_process_rules,
                "name": document.name,
                "created_from": document.created_from,
                "created_by": document.created_by,
                "created_at": document.created_at.timestamp(),
                "tokens": document.tokens,
                "indexing_status": document.indexing_status,
                "completed_at": int(document.completed_at.timestamp()) if document.completed_at else None,
                "updated_at": int(document.updated_at.timestamp()) if document.updated_at else None,
                "indexing_latency": document.indexing_latency,
                "error": document.error,
                "enabled": document.enabled,
                "disabled_at": int(document.disabled_at.timestamp()) if document.disabled_at else None,
                "disabled_by": document.disabled_by,
                "archived": document.archived,
                "doc_type": document.doc_type,
                "doc_metadata": document.doc_metadata_details,
                "segment_count": document.segment_count,
                "average_segment_length": document.average_segment_length,
                "hit_count": document.hit_count,
                "display_status": document.display_status,
                "doc_form": document.doc_form,
                "doc_language": document.doc_language,
            }

        return response


api.add_resource(
    DocumentAddByTextApi,
    "/datasets/<uuid:dataset_id>/document/create_by_text",
    "/datasets/<uuid:dataset_id>/document/create-by-text",
)
api.add_resource(
    DocumentAddByFileApi,
    "/datasets/<uuid:dataset_id>/document/create_by_file",
    "/datasets/<uuid:dataset_id>/document/create-by-file",
)
api.add_resource(
    DocumentUpdateByTextApi,
    "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/update_by_text",
    "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/update-by-text",
)
api.add_resource(
    DocumentUpdateByFileApi,
    "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/update_by_file",
    "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/update-by-file",
)
api.add_resource(DocumentDeleteApi, "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>")
api.add_resource(DocumentListApi, "/datasets/<uuid:dataset_id>/documents")
api.add_resource(DocumentIndexingStatusApi, "/datasets/<uuid:dataset_id>/documents/<string:batch>/indexing-status")
api.add_resource(DocumentDetailApi, "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>")
