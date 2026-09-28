import argparse
import logging

from dotenv import load_dotenv

from linkedin_bot.config import load_config
from linkedin_bot.runtime import Step, open_runtime, outcome_message, render_pending, resume, start_run
from linkedin_bot.state import Decision

CONSOLE_ACTIONS = {"f": "approve", "b": "edit", "u": "revise", "t": "new_topic", "v": "reject"}


def print_candidates(step: Step, top: int) -> None:
    state = step.state
    print(f"\n{len(state.get('items', []))} Items gesammelt, {len(state.get('unique', []))} neu nach Dedup.\n")
    print("Top-Kandidaten:")
    for s in state.get("scored", [])[:top]:
        print(f"  {s.relevance:>2}/10 [{s.category}] {s.item.title}\n         → {s.reason}")


def ask_decision() -> Decision:
    while True:
        choice = input("\n[f]reigeben  [b]earbeiten  [u]eberarbeiten lassen  anderes [t]hema  [v]erwerfen: ").strip().lower()
        action = CONSOLE_ACTIONS.get(choice)
        if action == "edit":
            print("Neuen Post-Text einfügen, Abschluss mit einer Zeile nur aus '.':")
            return Decision(action=action, text="\n".join(iter(input, ".")))
        if action == "revise":
            return Decision(action=action, text=input("Feedback an den Writer: "))
        if action:
            return Decision(action=action)


def run_console(args) -> None:
    """Kompletter Lauf im Terminal – Freigabe per Tastatur statt Telegram."""
    with open_runtime(load_config(), use_db=not args.no_db) as runtime:
        graph = runtime.graph
        step = start_run(graph)
        print_candidates(step, args.top)
        while step.pending:
            print(f"\n{'=' * 70}\n{render_pending(step.pending)}\n{'=' * 70}")
            step = resume(graph, step.thread_id, ask_decision())
        print("\n" + outcome_message(step.state))


def serve(_args) -> None:
    from linkedin_bot.telegram_bot import ApprovalBot

    cfg = load_config()
    with open_runtime(cfg) as runtime:
        ApprovalBot(cfg, runtime).run()


def linkedin_login_cmd(_args) -> None:
    from linkedin_bot.login import linkedin_login

    linkedin_login(load_config())


def main() -> None:
    parser = argparse.ArgumentParser(prog="linkedin-bot")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Ein Lauf im Terminal, Freigabe per Tastatur")
    run.add_argument("--top", type=int, default=5, help="Anzahl der Top-Kandidaten in der Ausgabe")
    run.add_argument("--no-db", action="store_true", help="Ohne Postgres (kein Dedup über Tage, nichts wird gespeichert)")
    run.set_defaults(func=run_console)

    commands.add_parser("serve", help="Telegram-Bot + täglicher Zeitplan").set_defaults(func=serve)
    commands.add_parser("linkedin-login", help="Bei LinkedIn anmelden (alle 60 Tage nötig)").set_defaults(func=linkedin_login_cmd)

    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx loggt jeden Request auf INFO – zu laut.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
