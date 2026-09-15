"""Trusted backend orchestration; not an unauthenticated browser upload endpoint.

The caller supplies an authenticated publisher and a verifier-owned source root.
Use a separate archive-service DB role for receipt insertion; publishers/ingestors
must not possess that EXECUTE permission. DB connections follow psycopg's DB-API.
"""
from __future__ import annotations

import json


def publish_with_source_archive(publication_db, archive_db, archiver, *, publisher_id,
                                package_digest, source_root, moderation_evidence,
                                public_source_approved=False):
    # Public listing and public source are separate decisions. Trusted callers
    # must obtain consent before this irreversible disclosure, not after upload.
    if public_source_approved is not True:
        raise PermissionError("public-source-approval-required")
    # Resolve owner, app and source digest from trusted, immutable Registry data.
    # Do not accept repository names, receipts or source hashes from a client.
    try:
        with publication_db.cursor() as cursor:
            cursor.execute("""SELECT app_id, source_digest_sha256
              FROM registry.source_archive_input_v1
              WHERE package_digest_sha256 = %s AND publisher_id = %s""",
                           (package_digest, publisher_id))
            row = cursor.fetchone()
        publication_db.commit()
    except Exception:
        publication_db.rollback()
        raise
    if row is None:
        raise PermissionError("release-not-owned-by-publisher")
    # No publication transaction is held across the network. Upload failure
    # leaves the release private, and retries revalidate the existing Git tag.
    receipt = archiver.upload_public(publisher_id, row[0], package_digest, row[1], source_root)
    try:
        with archive_db.cursor() as cursor:
            cursor.execute("SELECT registry.record_github_source_archive_v1(%s,%s::jsonb)",
                           (package_digest, json.dumps(receipt)))
        archive_db.commit()
    except Exception:
        archive_db.rollback()
        raise
    # The DB publication trigger is mandatory even for callers bypassing this
    # helper. Existing verification, revocation and consent checks still apply.
    try:
        with publication_db.cursor() as cursor:
            cursor.execute("SELECT registry.publish_release_v1(%s,%s)",
                           (package_digest, moderation_evidence))
        publication_db.commit()
    except Exception:
        publication_db.rollback()
        raise
    return {"publication_state": "published", "github_archive": receipt}
