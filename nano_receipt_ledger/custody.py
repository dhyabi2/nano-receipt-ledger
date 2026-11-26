"""The citable custody answer. Static, public, and deliberately not flattering."""

CUSTODY = {
    "claim": "non-custodial by construction",
    "structural_proof": {
        "what": "The counterparty's keypair is generated in the counterparty's own "
                "process and is never transmitted.",
        "check_it_yourself": "nano-wallet selfcheck",
        "asserts": [
            "create_address makes no network call",
            "no output field or log line can contain a seed or private key",
            "send is absent entirely in the receive-only profile",
        ],
        "spec": "specs/agent-tool-wallet-mcp.md",
    },
    "historical_proof": {
        "what": "Every outward transfer we have made is published with its Nano block hash.",
        "index": "/v1/receipts",
        "corrections": "/v1/corrections",
        "totals_are_confirmed_only": True,
    },
    "what_we_do_hold": "Our own funding account's key, which pays you. Nothing of yours.",
    "what_we_cannot_do": [
        "spend from your address",
        "freeze your balance",
        "reverse a confirmed send",
        "see your key",
    ],
}
