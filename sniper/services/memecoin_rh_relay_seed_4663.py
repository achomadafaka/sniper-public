"""4663 Relay fill authenticity seed (spec 2026-10-01-memecoin-4663-fast-lane-design.md §2.1).

SOLVERS: the senders of 194 watcher-proven Relay fills to armed wallets, 2026-09-14..10-01
(onchain_wallet_detections; tx.from read on the research QuickNode). PAYERS: the Permit2 payer of
all 194, one address: Relay pays from its own funds, never the wallet. The fast lane learns new
solvers only from fills the watcher has proved from receipts.
"""
SOLVERS = frozenset({
    "0x059df16bdcf8a6bd20bb4814518ff6a2df3c7b11",
    "0x0ec4e45f9ce3020024c0418a74cd31fbc487038f",
    "0x18dd3c14e34c1bc379f7538068c59160d9f68e25",
    "0x1cd9d560440aab96f7b0a007ced7c2191cac5baf",
    "0x2bb27b73c602643f42471d4751fe4fc4eebefbd1",
    "0x331d9a049d496385998067abf6cbb6371c8d2466",
    "0x344daadde53a81f95bcc46a99d39107a509251a1",
    "0x370a7e2d300c14d79d4a7ee07aaca46c4b3012cf",
    "0x4381045063b19a615a272733fa6b5f169b622646",
    "0x511808449be470efaf4131838b2d4998500a6f72",
    "0x56c262027e0de4aea31d2489529cb25d23e58a8b",
    "0x6085932878d587332419ac976fc13d98eba6fdab",
    "0x67d698a325f29e17a9477f28ba4fe50f1cec60e4",
    "0x69374f8233a4069d48f8994d84052ecae6277ef5",
    "0x698dd6c03b50c3d4af12932a0388338bcdd3aaed",
    "0x728f51950ff096dc03a48f5e83f45ac8bb75f500",
    "0x81adf99fcc6073157008e25010967d307875c13c",
    "0x919968a4238a54b381f022a360260ef5d7e015ac",
    "0x9bc462bce2acd6fbe2ef5470d55b439453451083",
    "0xa5a5491bca93dd4c076e4906e79e7673f4a5a142",
    "0xa67d7eb4dc68fa6ce8e34ef8cadaf075b9893fbb",
    "0xabb2acd3be814a80e502575d6c1dc5f789e9cd10",
    "0xada5bb90d0de0bd1b6f3938708f49295a8d1f7cb",
    "0xca7ded7e4f4ba8ab3b10009236ae6d1b95094589",
    "0xcc4bc7882d5ec4a3716e589ab3d816b61ae47a1a",
    "0xd460b4182ed80992d498290bcbc6b60e95de181b",
    "0xd782268f600403843484b70710daee6846fc0db8",
    "0xd9497b53abcd6972b6370cb195b6bfd6b280e607",
    "0xe209e0047731aa494289e1af9a0d03da19c5ef08",
    "0xf42b73d2e4d3912eade2a5f7ab39b54064c2acec",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef",
})
PAYERS = frozenset({
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef",
})
