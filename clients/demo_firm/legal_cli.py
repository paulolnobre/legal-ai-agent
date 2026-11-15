"""Legal assistant CLI: manage staff, ingest documents, and run queries.

Usage (from the project root):
    python clients/demo_firm/legal_cli.py add-staff "Dra. Ana" --chat-id 123456
    python clients/demo_firm/legal_cli.py ingest contrato.pdf --type contrato --matter "Caso X"
    python clients/demo_firm/legal_cli.py list
    python clients/demo_firm/legal_cli.py ask "Qual o prazo de rescisão?"
    python clients/demo_firm/legal_cli.py reprocess 7 8
"""
import argparse
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.repositories.connection import connect_async  # noqa: E402
from core.repositories.teams import Scope  # noqa: E402
from core.repositories.documents import list_documents  # noqa: E402
from core.repositories.staff import add_staff  # noqa: E402
from core.services.ingestion import ingest_file, reprocess_document  # noqa: E402
from core.services.legal_assistant import (  # noqa: E402
    answer_question,
    sources,
)


def _parser():
    parser = argparse.ArgumentParser(description="Legal assistant CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_staff = sub.add_parser("add-staff", help="Authorize a staff member")
    p_staff.add_argument("name")
    p_staff.add_argument("--chat-id")
    p_staff.add_argument("--email")
    p_staff.add_argument("--role", default="lawyer")

    p_ing = sub.add_parser("ingest", help="Ingest a document (PDF/DOCX/TXT)")
    p_ing.add_argument("path")
    p_ing.add_argument("--title")
    p_ing.add_argument("--type", dest="doc_type")
    p_ing.add_argument("--matter")

    sub.add_parser("list", help="List documents in the corpus")

    p_re = sub.add_parser(
        "reprocess",
        help="Extract stored originals again with the current extractors, in place",
    )
    p_re.add_argument("ids", nargs="+", type=int)

    p_ask = sub.add_parser("ask", help="Ask the corpus (with citations)")
    p_ask.add_argument("question")
    p_ask.add_argument("--matter")

    return parser


async def _main():
    args = _parser().parse_args()
    conn = await connect_async()
    try:
        if args.cmd == "add-staff":
            sid = await add_staff(conn, args.name, chat_id=args.chat_id,
                                  email=args.email, role=args.role)
            print(f"Staff authorized: id={sid}, {args.name}")

        elif args.cmd == "ingest":
            doc_id, n = await ingest_file(
                conn, args.path, title=args.title, doc_type=args.doc_type, matter=args.matter
            )
            if n == 0:
                print(f"Document already in corpus (id={doc_id}).")
            else:
                print(f"Ingested: id={doc_id}, {n} chunks.")

        elif args.cmd == "list":
            for d in await list_documents(conn, scope=Scope.unrestricted()):
                print(f"[{d.id}] {d.title}  ({d.doc_type or '-'} | {d.matter or '-'})")

        elif args.cmd == "reprocess":
            # One at a time, and each reported, so a failure halfway leaves a
            # clear record of which documents were corrected and which were not.
            for doc_id in args.ids:
                result = await reprocess_document(conn, doc_id)
                if result.changed:
                    print(f"[{doc_id}] reprocessed: {result.chars_before} -> "
                          f"{result.chars_after} chars, {result.chunks_after} chunks.")
                else:
                    print(f"[{doc_id}] unchanged: the extractors give the same text.")

        elif args.cmd == "ask":
            # The only way to read the corpus, here as in Telegram: a question,
            # answered from the passages that match it. There is no subcommand
            # that reads a stored document from end to end.
            _print_reply(await answer_question(conn, args.question, matter=args.matter))
    finally:
        await conn.close()


def _print_reply(reply) -> None:
    """On the terminal there is nobody to ask, so the sources are printed."""
    print(reply.text)
    if reply.sources:
        print()
        print(sources.listing(reply.sources))


if __name__ == "__main__":
    asyncio.run(_main())
