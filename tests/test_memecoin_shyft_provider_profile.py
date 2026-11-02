"""Offline provider selection and account-allocation refusal witnesses."""
from copy import deepcopy

import pytest


def shyft_url(key="research"):
    return "https://rpc.shyft.to/?api_key=" + key


def select(role, env):
    from sniper.services.memecoin_solana_provider import provider_urls
    return provider_urls(role, {"MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft", **env})


def allocation(role, account=None, scope=None):
    return dict(role=role, account_label=account or role, quota_scope_label=scope or role,
                rpc_rps=10, send_rps=5, grpc_connections=3, grpc_filters=5,
                region="ams", verified_at="2026-10-11T21:00:00+00:00",
                allocation_evidence="authenticated account allocation export")


def validate(record):
    from sniper.services.memecoin_solana_provider import validate_shyft_capacity
    return validate_shyft_capacity(record)


def test_profile_defaults_legacy_and_rejects_unknown_without_echo():
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    assert infra_provider({}) == "legacy"
    with pytest.raises(ValueError, match="provider_unknown") as exc:
        infra_provider({"MEMECOIN_SOLANA_INFRA_PROVIDER": "secret-value"})
    assert "secret-value" not in str(exc.value)
    with pytest.raises(ValueError, match="role_unknown"):
        provider_urls("typo", {})


@pytest.mark.parametrize("role,knob", [
    ("funded", "MEMECOIN_SOLANA_RPC_POOL_URLS"),
    ("signal", "ONCHAIN_WATCH_SOLANA_RPC_URL"),
    ("research", "MEMECOIN_RESEARCH_SOLANA_RPC_URLS"),
    ("read", "MEMECOIN_SOLANA_READ_RPC_URL"),
    ("tpu", "MEMECOIN_TPU_RPC_URL"),
    ("cohort", "MEMECOIN_SOL_COHORT_RPC_URL"),
])
def test_shyft_roles_require_explicit_authenticated_endpoints(role, knob):
    with pytest.raises(ValueError, match=role + "_missing"):
        select(role, {})
    env = {knob: shyft_url(role)}
    before = deepcopy(env)
    assert select(role, env) == (shyft_url(role),)
    assert env == before


def test_shyft_never_uses_legacy_research_alias():
    with pytest.raises(ValueError, match="research_missing"):
        select("research", {"MEMECOIN_RESEARCH_HELIUS_RPC_URL":
                            "https://legacy.invalid/?api-key=secret"})


@pytest.mark.parametrize("url", [
    "https://api.mainnet-beta.solana.com", "http://rpc.shyft.to/?api_key=secret",
    "https://evilshyft.to/?api_key=secret", "https://rpc.shyft.to.evil/?api_key=secret",
    "https://user:secret@rpc.shyft.to/?api_key=secret", "https://rpc.shyft.to/",
    "https://rpc.shyft.to/?api_key=", "https://rpc.shyft.to/?api_key=secret&api-key=other",
    "https://rpc.shyft.to/?api_key=secret&api_key=other", "https://[secret/?api_key=secret",
])
def test_shyft_invalid_urls_fail_without_credentials(url):
    with pytest.raises(ValueError) as exc:
        select("research", {"MEMECOIN_RESEARCH_SOLANA_RPC_URLS": url})
    assert "secret" not in str(exc.value)
    assert url not in str(exc.value)


def test_shyft_rejects_duplicate_credentials_even_when_urls_differ():
    with pytest.raises(ValueError, match="research_duplicate"):
        select("research", {"MEMECOIN_RESEARCH_SOLANA_RPC_URLS":
                            shyft_url() + "," + shyft_url() + "&region=ams"})


def test_shyft_research_refuses_exact_funded_key_but_not_other_keys():
    env = {"MEMECOIN_RESEARCH_SOLANA_RPC_URLS": shyft_url(),
           "MEMECOIN_SOLANA_RPC_URL": shyft_url() + "&region=ams"}
    with pytest.raises(ValueError, match="research_funded_overlap"):
        select("research", env)
    env["MEMECOIN_SOLANA_RPC_URL"] = shyft_url("funded")
    assert select("research", env) == (shyft_url(),)


def test_shyft_funded_pool_never_appends_public_fallback():
    with pytest.raises(ValueError, match="funded_url_invalid"):
        select("funded", {"MEMECOIN_SOLANA_RPC_POOL_URLS": shyft_url("funded"),
                          "MEMECOIN_SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com"})


def test_capacity_requires_actual_allocation_evidence():
    assert validate({}) == ("allocations_missing",)
    records = [allocation("funded"), allocation("research")]
    assert validate({"allocations": records}) == ()
    records[0]["allocation_evidence"] = ""
    assert "allocation_evidence_missing" in validate({"allocations": records})


@pytest.mark.parametrize("field,value", [
    ("account_label", ""), ("quota_scope_label", ""), ("rpc_rps", None),
    ("send_rps", -1), ("grpc_connections", 1.5), ("grpc_filters", True),
    ("region", ""), ("verified_at", "2026-10-11"), ("role", "secret-role"),
])
def test_capacity_refuses_unknown_or_malformed_fields_without_echo(field, value):
    row = allocation("funded")
    row[field] = value
    reasons = validate({"allocations": [row]})
    assert reasons and "secret-role" not in str(reasons)


@pytest.mark.parametrize("same_account", [False, True])
def test_different_key_labels_do_not_prove_quota_isolation(same_account):
    records = [allocation("funded", "account-a", "shared"),
               allocation("research", "account-a" if same_account else "account-b", "shared")]
    assert "quota_scope_shared" in validate({"allocations": records})
    for row in records:
        row["reserved_capacity_evidence"] = "provider-enforced role reservation export"
    assert validate({"allocations": records}) == ()


def test_one_account_with_two_scope_labels_still_requires_isolation_evidence():
    records = [allocation("funded", "shared"), allocation("research", "shared")]
    assert "account_shared" in validate({"allocations": records})


def test_legacy_single_url_preserves_commas_inside_query_and_ignores_neutral_alias():
    from sniper.services.memecoin_solana_provider import provider_urls
    url = "https://legacy.invalid/?api-key=legacy&features=a,b"
    assert provider_urls("research", {"MEMECOIN_RESEARCH_HELIUS_RPC_URL": url,
                                      "MEMECOIN_RESEARCH_SOLANA_RPC_URLS": shyft_url()}) == (url,)
    assert provider_urls("read", {"MEMECOIN_SOLANA_READ_RPC_URL": url}) == (url,)


@pytest.mark.parametrize("value", [[], {}, 1, None])
def test_capacity_malformed_role_is_a_safe_refusal(value):
    row = allocation("funded")
    row["role"] = value
    assert "role_invalid" in validate({"allocations": [row]})


def test_shyft_research_refuses_ambiguous_funded_credentials():
    with pytest.raises(ValueError, match="funded_url_invalid"):
        select("research", {"MEMECOIN_RESEARCH_SOLANA_RPC_URLS": shyft_url(),
                            "MEMECOIN_SOLANA_RPC_URL": shyft_url() + "&api-key=other"})


@pytest.mark.parametrize("host", ["rpc.ams.shyft.to", "rpc.fra.shyft.to"])
@pytest.mark.parametrize("role,knob", [
    ("funded", "MEMECOIN_SOLANA_RPC_POOL_URLS"),
    ("signal", "ONCHAIN_WATCH_SOLANA_RPC_URL"),
    ("research", "MEMECOIN_RESEARCH_SOLANA_RPC_URLS"),
    ("read", "MEMECOIN_SOLANA_READ_RPC_URL"),
    ("tpu", "MEMECOIN_TPU_RPC_URL"),
    ("cohort", "MEMECOIN_SOL_COHORT_RPC_URL"),
])
def test_authenticated_eu_rpc_hosts_are_accepted_for_each_role(host, role, knob):
    url = f"https://{host}/?api_key={role}-test"
    assert select(role, {knob: url}) == (url,)


@pytest.mark.parametrize("host", ["rpc.ams.shyft.to.evil", "evilrpc.fra.shyft.to",
                                 "rpc.nyc.shyft.to"])
def test_regional_rpc_allowlist_stays_exact(host):
    url = f"https://{host}/?api_key=secret"
    with pytest.raises(ValueError, match="research_url_invalid") as exc:
        select("research", {"MEMECOIN_RESEARCH_SOLANA_RPC_URLS": url})
    assert "secret" not in str(exc.value)


def test_regional_research_and_funded_hosts_do_not_isolate_the_same_key():
    with pytest.raises(ValueError, match="research_funded_overlap"):
        select("research", {
            "MEMECOIN_RESEARCH_SOLANA_RPC_URLS": "https://rpc.ams.shyft.to/?api_key=same",
            "MEMECOIN_SOLANA_RPC_URL": "https://rpc.fra.shyft.to/?api_key=same",
        })
