import logging

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..models import ConsentArtefact, ConsentReceipt, Partner
from ..utils.canonical import canonical_bytes, sha256_hex
from .crypto_service import CryptoService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class ReceiptService(BaseService):
    """Builds canonical artefact documents and CM-signed consent receipts."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.crypto = CryptoService.get_component()

    def artefact_document(self, artefact: ConsentArtefact) -> dict:
        """Canonical JSON-LD representation of a consent artefact (for hashing).

        An originated consent with several grants carries them under ``grants``
        (``data_controller`` is then the single controller, or None)."""
        doc = {
            "@context": "https://openg2p.org/contexts/consent_artefact.jsonld",
            "@type": "ConsentArtefact",
            "consent_id": artefact.id,
            "subject_id": artefact.subject_id_value,
            "subject_id_type": artefact.subject_id_type,
            "data_controller": artefact.controller_id,
            "partner_id": artefact.partner_id,
            "source": artefact.source,
            "purpose": artefact.purpose,
            "data_scopes": artefact.data_scopes,
            "effective_data_scopes": artefact.effective_data_scopes,
            "fetch_type": artefact.fetch_type,
            "policy_version": artefact.policy_version,
            "validity": {
                "consent_timestamp": artefact.valid_from.isoformat(),
                "expiry_timestamp": artefact.valid_until.isoformat(),
            },
        }
        if artefact.grants is not None:
            doc["grants"] = artefact.grants
        return doc

    @staticmethod
    def _controllers(artefact: ConsentArtefact) -> list:
        """Receipt data controllers: one per grant, or the artefact's single one.
        (Kantara / ISO 27560 receipts allow several controllers.)"""
        if artefact.grants:
            return [
                {
                    "id": g["data_controller"],
                    "data_categories": g.get("effective_data_scopes") or [],
                }
                for g in artefact.grants
                if g.get("effective_data_scopes")
            ]
        return [{"id": artefact.controller_id}]

    def build_receipt(self, artefact: ConsentArtefact, partner: Partner) -> ConsentReceipt:
        artefact_hash = sha256_hex(canonical_bytes(self.artefact_document(artefact)))
        signature_value = self.crypto.sign(artefact_hash.encode("utf-8"))

        document = {
            "@context": "https://openg2p.org/contexts/consent_receipt.jsonld",
            "@type": "ConsentReceipt",
            "version": "1.1",
            "issued_at": artefact.created_at.isoformat()
            if artefact.created_at
            else None,
            # Single controller (embedded consents are one artefact per
            # controller); `data_controllers` lists all of them — several for an
            # originated consent with grants.
            "data_controller": {"id": artefact.controller_id}
            if artefact.controller_id
            else None,
            "data_controllers": self._controllers(artefact),
            "subject_id": artefact.subject_id_value,
            "purposes": [
                {
                    **(artefact.purpose or {}),
                    "legal_basis": "consent",
                    "data_categories": artefact.effective_data_scopes,
                }
            ],
            "data_categories": artefact.effective_data_scopes,
            "third_parties": [partner.audience],
            "withdrawal": {
                "method": f"POST /consent/v1/consents/{artefact.id}/revoke"
            },
            "consent_artefact": {"@id": artefact.id, "hash": artefact_hash},
            "signature": {
                "algorithm": self.crypto.algorithm,
                "kid": self.crypto.kid,
                "value": signature_value,
            },
        }

        return ConsentReceipt(
            consent_id=artefact.id,
            artefact_hash=artefact_hash,
            algorithm=self.crypto.algorithm,
            kid=self.crypto.kid,
            signature=signature_value,
            version="1.1",
            document=document,
        )
