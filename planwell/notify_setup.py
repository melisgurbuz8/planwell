"""Set up phone notifications (ntfy) in one step.

    python3 -m planwell.notify_setup            # new random topic -> secrets/ntfy.json, then a test message
    python3 -m planwell.notify_setup --test     # just send a test message to the saved topic
    python3 -m planwell.notify_setup --keep     # keep an existing topic (re-print instructions + test)

The topic works like a password: anyone who knows it can read the messages, so it is random,
stored only in secrets/ntfy.json (0600) and never in config.json.
Optional: put an ntfy access token in secrets/ntfy_token.txt (or env NTFY_TOKEN) for a
reserved/private topic; it is sent as `Authorization: Bearer ...`.
"""
import argparse
import secrets
import sys

from planwell import notify as nt
from planwell.settings import load_config


def new_topic():
    """32 random URL-safe characters ([A-Za-z0-9_-])."""
    return "pw_" + secrets.token_urlsafe(22)[:29]


def instructions(topic, server):
    host = server.replace("https://", "").rstrip("/")
    return "\n".join([
        "",
        "On your phone:",
        "  1. Install the free ntfy app (App Store / Google Play).",
        "  2. Tap + (Subscribe to topic).",
        "  3. Topic: %s" % topic,
        "  4. Server: %s%s" % (host, "  (the default - leave 'use another server' off)" if host == "ntfy.sh" else
                               "  (turn on 'use another server' and enter it)"),
        "  5. Allow notifications for ntfy.",
        "",
        "Keep this topic private - it works like a password. It is saved in secrets/ntfy.json.",
    ])


def send_test(topic, config, publish=None, secrets_dir=None):
    title = "PlanWell is connected"
    body = ("Test notification. Your walk, Solidcore and padel checks will arrive here on time, "
            "even while your Mac is asleep. Tap to open PlanWell on your Mac.")
    pub = publish or (lambda *a, **k: nt.publish(*a, **k))
    return pub(topic, title, body, "green", nt.server_for(config), token=nt.load_token(secrets_dir), log=print)


def main(argv=None, secrets_dir=None, publish=None):
    ap = argparse.ArgumentParser(description="Set up PlanWell phone notifications (ntfy)")
    ap.add_argument("--test", action="store_true", help="only send a test notification")
    ap.add_argument("--keep", action="store_true", help="keep the existing topic if there is one")
    args = ap.parse_args(argv)
    config = load_config()
    server = nt.server_for(config)
    topic = nt.load_topic(secrets_dir)
    if args.test:
        if not topic:
            print("No ntfy topic yet - run: python3 -m planwell.notify_setup")
            return 1
    elif not (args.keep and topic):
        topic = new_topic()
        nt.save_topic(topic, secrets_dir)
        print("Saved a new private topic to secrets/ntfy.json.")
    if not args.test:
        print(instructions(topic, server))
    if nt.load_token(secrets_dir):
        print("Using the access token from NTFY_TOKEN / secrets/ntfy_token.txt.")
    ok = send_test(topic, config, publish, secrets_dir)
    print("\nTest notification %s." % ("sent - check your phone" if ok else
                                        "could NOT be sent (check your internet connection and try --test)"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
