"""Air-gapped Admin CLI tool (`rag-admin`, `services/admin_cli.py`).

Canonical specification - `core/admin_panel.md` §10.
Duplicates admin functions for closed/air-gapped contours where web admin port is inaccessible:
- `rag-admin license activate --file /etc/rag/license.key`
- `rag-admin license status [--json]`
- `rag-admin ingest --path ./data --access-level 0`
- `rag-admin documents list|delete|reindex|reclassify`
- `rag-admin admin-user create|list|delete`

Exit codes:
- 0: Success
- 1: Validation / input error
- 2: License error (invalid / expired / Restricted Mode)
- 3: Internal error
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from rag_core.admin.service import AdminService
from rag_core.contracts.enums import AccessLevel, AdminRole, DocumentStatus
from rag_core.contracts.models import LicenseActivationRequest
from rag_core.contracts.settings import Settings
from rag_core.db.session import build_session_factory

# Exit codes per core/admin_panel.md §10
EXIT_SUCCESS = 0
EXIT_VALIDATION_ERROR = 1
EXIT_LICENSE_ERROR = 2
EXIT_INTERNAL_ERROR = 3

CLI_ACTOR_EMAIL = "cli-operator@localhost"


class AdminCLI:
    """Air-gapped CLI runner backed by shared AdminService."""

    def __init__(
        self, service: AdminService | None = None, settings: Settings | None = None
    ) -> None:
        self.settings = settings or Settings()
        session_factory = build_session_factory(self.settings.postgres_dsn)
        self.service = service or AdminService(
            session_factory=session_factory, settings=self.settings
        )

    async def license_status(self, tenant_id: UUID, as_json: bool = False) -> int:
        try:
            status = await self.service.get_license_status(tenant_id)
            if as_json:
                print(json.dumps(status.model_dump(mode="json"), indent=2))
            else:
                print("=== License Status ===")
                print(f"Valid: {status.is_valid}")
                print(f"Plan: {status.plan}")
                print(f"Mode: {status.mode}")
                print(f"Expires at: {status.expires_at or 'Perpetual'}")
                print(f"Documents: {status.used_documents} / {status.max_documents}")
                print(f"Users: {status.used_users} / {status.max_users}")
                if status.restricted_mode:
                    print("⚠️ RESTRICTED MODE: License invalid or expired")
            if status.restricted_mode or not status.is_valid:
                return EXIT_LICENSE_ERROR
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error fetching license status: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    async def license_activate(
        self,
        tenant_id: UUID,
        code: str | None = None,
        token: str | None = None,
        file_path: str | None = None,
        as_json: bool = False,
    ) -> int:
        if not code and not token and not file_path:
            print("Error: Must provide one of --code, --token, or --file", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

        file_b64: str | None = None
        if file_path:
            p = Path(file_path)
            if not p.is_file():
                print(f"Error: File '{file_path}' does not exist", file=sys.stderr)
                return EXIT_VALIDATION_ERROR
            raw_content = p.read_bytes()
            # If it's already plain text, encode to b64 for activation request
            file_b64 = base64.b64encode(raw_content).decode("ascii")

        req = LicenseActivationRequest(
            activation_code=code,
            license_token=token,
            license_file_b64=file_b64,
        )

        try:
            status = await self.service.activate_license(
                tenant_id=tenant_id,
                request=req,
                actor_email=CLI_ACTOR_EMAIL,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
            )
            if as_json:
                print(json.dumps(status.model_dump(mode="json"), indent=2))
            else:
                print("License activated successfully.")
                print(f"Plan: {status.plan}, Mode: {status.mode}, Valid: {status.is_valid}")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error activating license: {exc}", file=sys.stderr)
            return EXIT_LICENSE_ERROR

    async def license_fingerprint(self, as_json: bool = False) -> int:
        try:
            fp = self.service.get_machine_fingerprint()
            if as_json:
                print(json.dumps(fp.model_dump(mode="json"), indent=2))
            else:
                print("=== Machine Fingerprint ===")
                print(f"Hardware ID: {fp.hardware_id}")
                print(f"Hostname: {fp.hostname}")
                print(f"Collected at: {fp.collected_at}")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error computing machine fingerprint: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    async def ingest_directory(
        self,
        tenant_id: UUID,
        directory_path: str,
        access_level: AccessLevel = AccessLevel.PUBLIC,
        recursive: bool = True,
        as_json: bool = False,
    ) -> int:
        p = Path(directory_path)
        if not p.is_dir():
            print(f"Error: Directory '{directory_path}' not found", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

        pattern = "**/*" if recursive else "*"
        files = [f for f in p.glob(pattern) if f.is_file() and not f.name.startswith(".")]

        if not files:
            print(f"No files found to ingest in '{directory_path}'")
            return EXIT_SUCCESS

        uploaded_count = 0
        results: list[dict[str, Any]] = []

        for file_path in files:
            try:
                content = file_path.read_bytes()
                item = await self.service.upload_document(
                    tenant_id=tenant_id,
                    file_name=file_path.name,
                    content=content,
                    access_level=access_level,
                    actor_email=CLI_ACTOR_EMAIL,
                    ip_address="127.0.0.1",
                    user_agent="rag-admin-cli",
                )
                uploaded_count += 1
                results.append(
                    {
                        "file": str(file_path),
                        "document_id": str(item.id),
                        "status": str(item.status),
                    }
                )
                if not as_json:
                    print(f"Uploaded: {file_path.name} -> ID: {item.id} [pending]")
            except Exception as exc:
                print(f"Failed to upload {file_path.name}: {exc}", file=sys.stderr)

        if as_json:
            print(json.dumps({"total_uploaded": uploaded_count, "items": results}, indent=2))
        else:
            print(f"\nIngest scheduled: {uploaded_count}/{len(files)} files queued.")

        return EXIT_SUCCESS

    async def list_documents(
        self,
        tenant_id: UUID,
        page: int = 1,
        per_page: int = 20,
        status: DocumentStatus | None = None,
        as_json: bool = False,
    ) -> int:
        try:
            docs_page = await self.service.list_documents(
                tenant_id=tenant_id,
                page=page,
                per_page=per_page,
                status=status,
            )
            if as_json:
                print(json.dumps(docs_page.model_dump(mode="json"), indent=2))
            else:
                p_info = f"Page {docs_page.page}/{docs_page.pages}, Total: {docs_page.total}"
                print(f"Documents ({p_info}):")
                for d in docs_page.items:
                    print(
                        f"- [{d.status.upper()}] ID: {d.id} | {d.file_name} | "
                        f"Level: {d.access_level} | Parents: {d.parent_chunk_count} | "
                        f"Children: {d.child_chunk_count}"
                    )
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error listing documents: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    async def delete_document(self, tenant_id: UUID, document_id: UUID) -> int:
        try:
            await self.service.delete_document(
                tenant_id=tenant_id,
                document_id=document_id,
                actor_email=CLI_ACTOR_EMAIL,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
            )
            print(f"Document {document_id} deleted successfully.")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error deleting document: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    async def reindex_document(self, tenant_id: UUID, document_id: UUID) -> int:
        try:
            await self.service.reindex_document(
                tenant_id=tenant_id,
                document_id=document_id,
                actor_email=CLI_ACTOR_EMAIL,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
            )
            print(f"Document {document_id} reindexing scheduled.")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error reindexing document: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    async def reclassify_document(
        self, tenant_id: UUID, document_id: UUID, access_level: AccessLevel
    ) -> int:
        try:
            await self.service.reclassify_document(
                tenant_id=tenant_id,
                document_id=document_id,
                new_access_level=access_level,
                actor_email=CLI_ACTOR_EMAIL,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
            )
            print(
                f"Document {document_id} and all related chunks "
                f"reclassified to access_level={int(access_level)}."
            )
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error reclassifying document: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    async def create_admin_user(
        self,
        email: str,
        password: str,
        role: AdminRole = AdminRole.VIEWER,
        tenant_id: UUID | None = None,
        as_json: bool = False,
    ) -> int:
        try:
            user = self.service.create_admin_user_sync(
                email=email,
                password=password,
                role=role,
                tenant_id=tenant_id,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
                actor_email=CLI_ACTOR_EMAIL,
            )
            if as_json:
                print(json.dumps(user.model_dump(mode="json"), indent=2))
            else:
                print("Admin user created successfully:")
                print(f"ID: {user.id}")
                print(f"Email: {user.email}")
                print(f"Role: {user.role}")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error creating admin user: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    async def list_admin_users(
        self, tenant_id: UUID | None = None, page: int = 1, as_json: bool = False
    ) -> int:
        try:
            users_page = self.service.list_admin_users_sync(tenant_id=tenant_id, page=page)
            if as_json:
                print(json.dumps(users_page.model_dump(mode="json"), indent=2))
            else:
                p_info = f"Page {users_page.page}/{users_page.pages}, Total: {users_page.total}"
                print(f"Admin Users ({p_info}):")
                for u in users_page.items:
                    print(
                        f"- ID: {u.id} | {u.email} | Role: {u.role} | "
                        f"Active: {u.is_active} | 2FA: {u.totp_enabled}"
                    )
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error listing admin users: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    async def delete_admin_user(self, user_id: UUID) -> int:
        try:
            self.service.delete_admin_user_sync(
                user_id=user_id,
                ip_address="127.0.0.1",
                user_agent="rag-admin-cli",
                actor_email=CLI_ACTOR_EMAIL,
            )
            print(f"Admin user {user_id} deleted successfully.")
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error deleting admin user: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR


def main() -> None:
    """CLI Entrypoint supporting Typer and standard argv parsing."""
    try:
        import typer

        app = typer.Typer(name="rag-admin", help="Air-gapped enterprise admin CLI")
        license_app = typer.Typer(name="license", help="License management commands")
        docs_app = typer.Typer(name="documents", help="Documents management commands")
        admin_user_app = typer.Typer(name="admin-user", help="Admin users management commands")

        cli_runner = AdminCLI()

        @license_app.command("status")
        def lic_status(
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            code = asyncio.run(cli_runner.license_status(UUID(tenant_id), as_json=as_json))
            sys.exit(code)

        @license_app.command("activate")
        def lic_activate(
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
            code: str | None = typer.Option(None, "--code", help="Activation code"),
            token: str | None = typer.Option(None, "--token", help="Signed license token"),
            file: str | None = typer.Option(None, "--file", help="Path to .key license file"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            res_code = asyncio.run(
                cli_runner.license_activate(
                    UUID(tenant_id), code=code, token=token, file_path=file, as_json=as_json
                )
            )
            sys.exit(res_code)

        @license_app.command("fingerprint")
        def lic_fingerprint(
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            code = asyncio.run(cli_runner.license_fingerprint(as_json=as_json))
            sys.exit(code)

        @app.command("ingest")
        def ingest(
            path: str = typer.Option(..., "--path", help="Directory path with documents"),
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
            access_level: int = typer.Option(0, "--access-level", help="Access level 0-3"),
            recursive: bool = typer.Option(True, "--recursive", help="Recursive scan"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            code = asyncio.run(
                cli_runner.ingest_directory(
                    tenant_id=UUID(tenant_id),
                    directory_path=path,
                    access_level=AccessLevel(access_level),
                    recursive=recursive,
                    as_json=as_json,
                )
            )
            sys.exit(code)

        @docs_app.command("list")
        def docs_list(
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
            page: int = typer.Option(1, "--page", help="Page number"),
            per_page: int = typer.Option(20, "--per-page", help="Items per page"),
            status: str | None = typer.Option(None, "--status", help="Filter by status"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            status_enum = DocumentStatus(status) if status else None
            code = asyncio.run(
                cli_runner.list_documents(
                    UUID(tenant_id),
                    page=page,
                    per_page=per_page,
                    status=status_enum,
                    as_json=as_json,
                )
            )
            sys.exit(code)

        @docs_app.command("delete")
        def docs_delete(
            document_id: str = typer.Argument(..., help="Document UUID to delete"),
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
        ) -> None:
            code = asyncio.run(
                cli_runner.delete_document(tenant_id=UUID(tenant_id), document_id=UUID(document_id))
            )
            sys.exit(code)

        @docs_app.command("reindex")
        def docs_reindex(
            document_id: str = typer.Argument(..., help="Document UUID to reindex"),
            tenant_id: str = typer.Option(str(uuid4()), "--tenant-id", help="Tenant UUID"),
        ) -> None:
            code = asyncio.run(
                cli_runner.reindex_document(
                    tenant_id=UUID(tenant_id), document_id=UUID(document_id)
                )
            )
            sys.exit(code)

        @admin_user_app.command("create")
        def user_create(
            email: str = typer.Option(..., "--email", help="Admin email"),
            password: str = typer.Option(..., "--password", help="Admin password"),
            role: str = typer.Option("viewer", "--role", help="Admin role"),
            tenant_id: str | None = typer.Option(None, "--tenant-id", help="Tenant UUID"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            t_id = UUID(tenant_id) if tenant_id else None
            code = asyncio.run(
                cli_runner.create_admin_user(
                    email=email,
                    password=password,
                    role=AdminRole(role),
                    tenant_id=t_id,
                    as_json=as_json,
                )
            )
            sys.exit(code)

        @admin_user_app.command("list")
        def user_list(
            tenant_id: str | None = typer.Option(None, "--tenant-id", help="Tenant UUID"),
            page: int = typer.Option(1, "--page", help="Page number"),
            as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
        ) -> None:
            t_id = UUID(tenant_id) if tenant_id else None
            code = asyncio.run(
                cli_runner.list_admin_users(tenant_id=t_id, page=page, as_json=as_json)
            )
            sys.exit(code)

        @admin_user_app.command("delete")
        def user_delete(
            user_id: str = typer.Argument(..., help="Admin User UUID to delete"),
        ) -> None:
            code = asyncio.run(cli_runner.delete_admin_user(user_id=UUID(user_id)))
            sys.exit(code)

        app.add_typer(license_app)
        app.add_typer(docs_app)
        app.add_typer(admin_user_app)

        app()
        return
    except ImportError:
        pass

    # Direct fallback runner when Typer is not installed
    cli = AdminCLI()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: rag-admin <command> [options]")
        print("Commands: license, ingest, documents, admin-user")
        sys.exit(EXIT_SUCCESS)

    cmd = args[0]
    default_tenant = uuid4()

    if cmd == "license":
        subcmd = args[1] if len(args) > 1 else "status"
        if subcmd == "status":
            sys.exit(asyncio.run(cli.license_status(default_tenant, as_json="--json" in args)))
        elif subcmd == "fingerprint":
            sys.exit(asyncio.run(cli.license_fingerprint(as_json="--json" in args)))
        elif subcmd == "activate":
            token_val = None
            file_val = None
            code_val = None
            for i, a in enumerate(args):
                if a == "--token" and i + 1 < len(args):
                    token_val = args[i + 1]
                elif a == "--file" and i + 1 < len(args):
                    file_val = args[i + 1]
                elif a == "--code" and i + 1 < len(args):
                    code_val = args[i + 1]
            sys.exit(
                asyncio.run(
                    cli.license_activate(
                        default_tenant,
                        code=code_val,
                        token=token_val,
                        file_path=file_val,
                        as_json="--json" in args,
                    )
                )
            )

    elif cmd == "ingest":
        path_val = "."
        for i, a in enumerate(args):
            if a == "--path" and i + 1 < len(args):
                path_val = args[i + 1]
        sys.exit(
            asyncio.run(cli.ingest_directory(default_tenant, path_val, as_json="--json" in args))
        )

    elif cmd == "documents":
        subcmd = args[1] if len(args) > 1 else "list"
        if subcmd == "list":
            sys.exit(asyncio.run(cli.list_documents(default_tenant, as_json="--json" in args)))
        elif subcmd == "delete" and len(args) > 2:
            sys.exit(asyncio.run(cli.delete_document(default_tenant, UUID(args[2]))))
        elif subcmd == "reindex" and len(args) > 2:
            sys.exit(asyncio.run(cli.reindex_document(default_tenant, UUID(args[2]))))

    elif cmd == "admin-user":
        subcmd = args[1] if len(args) > 1 else "list"
        if subcmd == "list":
            sys.exit(asyncio.run(cli.list_admin_users(default_tenant, as_json="--json" in args)))
        elif subcmd == "delete" and len(args) > 2:
            sys.exit(asyncio.run(cli.delete_admin_user(UUID(args[2]))))

    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(EXIT_VALIDATION_ERROR)


if __name__ == "__main__":
    main()
