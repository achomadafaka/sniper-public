"""Build one USDC transfer from the funded Solana wallet (2026-09-13, operator withdrawals).

Shared by scripts/memecoin_withdraw_solana_usdc.py (operator-run) and the runner's dashboard
withdrawal (memecoin_withdraw). One builder, so both send byte-identical instructions.
"""
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TRANSFER_CHECKED_IX = 12
CREATE_IDEMPOTENT_IX = 1


def associated_token_account(owner, mint=USDC_MINT):
    from solders.pubkey import Pubkey
    owner, mint = Pubkey.from_string(str(owner)), Pubkey.from_string(mint)
    return Pubkey.find_program_address(
        [bytes(owner), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(mint)],
        Pubkey.from_string(ATA_PROGRAM))[0]


def withdraw_instructions(sender, recipient, amount_atomic, *, create_recipient_account):
    """Bounded priority, [create the recipient's USDC account (we pay)], TransferChecked."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    sender, recipient = Pubkey.from_string(str(sender)), Pubkey.from_string(str(recipient))
    mint = Pubkey.from_string(USDC_MINT)
    token = Pubkey.from_string(TOKEN_PROGRAM)
    source, dest = associated_token_account(sender), associated_token_account(recipient)
    # The fixed priority fee is at most 100,000 lamports (0.0001 SOL), plus base fee and rent.
    out = [set_compute_unit_limit(50_000), set_compute_unit_price(2_000_000)]
    if create_recipient_account:
        out.append(Instruction(
            program_id=Pubkey.from_string(ATA_PROGRAM),
            accounts=[AccountMeta(sender, True, True), AccountMeta(dest, False, True),
                      AccountMeta(recipient, False, False), AccountMeta(mint, False, False),
                      AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM), False, False),
                      AccountMeta(token, False, False)],
            data=bytes([CREATE_IDEMPOTENT_IX])))
    out.append(Instruction(
        program_id=token,
        accounts=[AccountMeta(source, False, True), AccountMeta(mint, False, False),
                  AccountMeta(dest, False, True), AccountMeta(sender, True, False)],
        data=bytes([TRANSFER_CHECKED_IX]) + int(amount_atomic).to_bytes(8, "little")
        + bytes([USDC_DECIMALS])))
    return out


def valid_wallet(text):
    """A wallet key is ON the ed25519 curve. A token account or other program address is not;
    sending to one would create a USDC account nobody can sign for."""
    try:
        from solders.pubkey import Pubkey
        return Pubkey.from_string(str(text)).is_on_curve()
    except Exception:
        return False


def build_signed_transfer(keypair, sender, recipient, amount_atomic, *, rpc_url, call_fn):
    """(signed_b64, sig, last_valid_height, created_recipient_account). Raises on a read failure."""
    import base64
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    info = call_fn(rpc_url, "getAccountInfo",
                   [str(associated_token_account(recipient)), {"encoding": "base64"}])
    result = info.get("result") if isinstance(info, dict) else None
    if (not isinstance(result, dict) or "value" not in result or info.get("error") is not None
            or (result["value"] is not None and not isinstance(result["value"], dict))):
        raise RuntimeError("could not read the recipient's USDC account")
    create = result["value"] is None
    block = call_fn(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    result = block.get("result") if isinstance(block, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict) or block.get("error") is not None:
        raise RuntimeError("no recent blockhash")
    if not value.get("blockhash"):
        raise RuntimeError("no recent blockhash")
    last_valid = value.get("lastValidBlockHeight")
    if type(last_valid) is not int or last_valid <= 0:
        raise RuntimeError("invalid last valid block height")
    blockhash = Hash.from_string(value["blockhash"])
    message = Message.new_with_blockhash(
        withdraw_instructions(sender, recipient, amount_atomic, create_recipient_account=create),
        Pubkey.from_string(str(sender)), blockhash)
    tx = Transaction([keypair], message, blockhash)
    return (base64.b64encode(bytes(tx)).decode(), str(tx.signatures[0]),
            last_valid, create)
