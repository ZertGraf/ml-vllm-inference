#!/usr/bin/env bash
# End-to-end checks of a running deployment:
#   1. authentication (no token / wrong token / right token, including the
#      routes vLLM's own --api-key leaves open)
#   2. a real chat completion (plain and streamed)
#   3. privacy: unique canary strings are sent in prompts, stop strings, bad
#      payloads and error paths; the model is asked to echo a canary; then the
#      logs of every container (and --previous) must not contain any canary or
#      the generated answer, while the request-fact lines must be present.
#
# Usage: scripts/verify-deployment.sh        (needs kubectl, curl, jq)
# Env:   NS=vllm  PORT=18000  MODEL=qwen2.5-0.5b-instruct
#        SECRETS=k8s/overlays/minikube/secrets.env
set -uo pipefail

NS="${NS:-vllm}"
PORT="${PORT:-18000}"
MODEL="${MODEL:-qwen2.5-0.5b-instruct}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS="${SECRETS:-${ROOT}/k8s/overlays/minikube/secrets.env}"
TOKEN="$(grep '^VLLM_API_KEY=' "${SECRETS}" | cut -d= -f2-)"
BASE="http://127.0.0.1:${PORT}"
TMP="$(mktemp -d)"
RID="$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
C_PROMPT="CANARY-PROMPT-${RID}"
C_OUT="CANARY-OUT-${RID}"
C_STOP="CANARY-STOP-${RID}"
C_ERR="CANARY-ERR-${RID}"
fails=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
expect_code() { # name expected actual
  if [ "$2" = "$3" ]; then pass "$1 -> HTTP $3"; else fail "$1 -> HTTP $3 (expected $2)"; fi
}
post() { # outfile, body, [extra curl args...]; echoes the HTTP code
  local out="$1" body="$2"; shift 2
  printf '%s' "${body}" > "${TMP}/request-body.json"   # builtin: no ARG_MAX limit
  curl -s -o "${out}" -w '%{http_code}' -X POST "${BASE}/v1/chat/completions" \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' "$@" \
    --data-binary "@${TMP}/request-body.json"
}

kubectl -n "${NS}" port-forward svc/vllm "${PORT}:8000" >"${TMP}/pf.log" 2>&1 &
PF=$!
trap 'kill ${PF} 2>/dev/null; rm -rf "${TMP}"' EXIT
for _ in $(seq 1 30); do curl -s -o /dev/null "${BASE}/health" && break; sleep 1; done

echo "== authentication"
expect_code "GET /health without token (public, used by probes)" 200 \
  "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/health")"
expect_code "GET /v1/models without token" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/v1/models")"
expect_code "GET /v1/models with wrong token" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong' "${BASE}/v1/models")"
expect_code "GET /v1/models with correct token" 200 \
  "$(curl -s -o "${TMP}/models.json" -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" "${BASE}/v1/models")"
echo "      model: $(jq -r '.data[0] | "\(.id) max_model_len=\(.max_model_len)"' "${TMP}/models.json")"
expect_code "POST /invocations without token (open in stock vLLM)" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/invocations" -H 'Content-Type: application/json' -d '{}')"
expect_code "POST /pause without token (open in stock vLLM)" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/pause")"
expect_code "GET /metrics without token" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/metrics")"

echo "== inference"
body=$(jq -n --arg m "${MODEL}" --arg p "Remember this tag: ${C_PROMPT}. Now repeat exactly this text and nothing else: ${C_OUT}" \
  --arg s "${C_STOP}" '{model:$m, messages:[{role:"user",content:$p}], max_tokens:48, temperature:0, stop:[$s]}')
code=$(post "${TMP}/chat.json" "${body}")
expect_code "chat completion (non-stream)" 200 "${code}"
ANSWER="$(jq -r '.choices[0].message.content' "${TMP}/chat.json")"
echo "      answer: ${ANSWER}"
echo "      usage : $(jq -c .usage "${TMP}/chat.json")"
if echo "${ANSWER}" | grep -q "${C_OUT}"; then pass "model echoed the output canary (canary check is not vacuous)"
else fail "model did not echo the output canary; output-leak check is weaker"; fi

body=$(jq -n --arg m "${MODEL}" --arg p "Count from one to five. Tag ${C_PROMPT}-stream" \
  '{model:$m, messages:[{role:"user",content:$p}], max_tokens:40, stream:true}')
code=$(post "${TMP}/stream.txt" "${body}")
expect_code "chat completion (stream)" 200 "${code}"
if grep -q '^data: \[DONE\]' "${TMP}/stream.txt"; then pass "stream terminated with [DONE]"; else fail "stream has no [DONE]"; fi

echo "== error paths (every payload carries a canary)"
yes "${C_ERR}-long" | head -n 6000 | tr '\n' ' ' > "${TMP}/long.txt"
expect_code "prompt longer than max-model-len" 400 \
  "$(post "${TMP}/e1.json" "$(jq -n --arg m "${MODEL}" --rawfile p "${TMP}/long.txt" '{model:$m,messages:[{role:"user",content:$p}],max_tokens:8}')")"
expect_code "malformed JSON body" 400 \
  "$(post "${TMP}/e2.json" "{\"model\": \"${MODEL}\", \"messages\": [{\"role\": \"user\", \"content\": \"${C_ERR}-json\" ")"
expect_code "wrong field type" 400 \
  "$(post "${TMP}/e3.json" "$(jq -n --arg m "${MODEL}" --arg v "${C_ERR}-type" '{model:$m,messages:[{role:"user",content:"hi"}],max_tokens:$v}')")"
expect_code "unknown model name" 404 \
  "$(post "${TMP}/e4.json" "$(jq -n --arg m "${C_ERR}-model" '{model:$m,messages:[{role:"user",content:"hi"}]}')")"
expect_code "messages is a string, not a list" 400 \
  "$(post "${TMP}/e5.json" "$(jq -n --arg m "${MODEL}" --arg r "${C_ERR}-messages" '{model:$m,messages:$r}')")"
expect_code "invalid json_schema" 400 \
  "$(post "${TMP}/e6.json" "$(jq -n --arg m "${MODEL}" --arg t "${C_ERR}-schema" '{model:$m,messages:[{role:"user",content:"hi"}],response_format:{type:"json_schema",json_schema:{name:"x",schema:{type:$t}}}}')")"
# client aborts a long streamed generation after one second
body=$(jq -n --arg m "${MODEL}" --arg p "Write a very long story. Tag ${C_PROMPT}-abort" \
  '{model:$m, messages:[{role:"user",content:$p}], max_tokens:400, ignore_eos:true, stream:true}')
post "${TMP}/abort.txt" "${body}" --max-time 1 >/dev/null 2>&1
pass "aborted stream sent (client disconnect)"
sleep 3

echo "== log scan (all containers, current and previous)"
: > "${TMP}/all.log"
for pod in $(kubectl -n "${NS}" get pods -l app.kubernetes.io/name=vllm -o name); do
  for c in $(kubectl -n "${NS}" get "${pod}" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}'); do
    kubectl -n "${NS}" logs "${pod}" -c "${c}" >>"${TMP}/all.log" 2>&1
    kubectl -n "${NS}" logs "${pod}" -c "${c}" --previous >>"${TMP}/all.log" 2>&1 || true
  done
done
echo "      scanned $(wc -l <"${TMP}/all.log") log lines"
if grep -q "CANARY" "${TMP}/all.log"; then
  fail "a canary string appears in the logs:"; grep -n "CANARY" "${TMP}/all.log" | head -5 | cut -c1-240
else pass "no canary (prompt / output / stop / error payloads) in any log"; fi
snippet="$(echo "${ANSWER}" | head -c 30)"
if [ -n "${snippet}" ] && grep -qF -- "${snippet}" "${TMP}/all.log"; then
  fail "generated answer text appears in the logs"
else pass "generated answer text not in the logs"; fi
facts=$(grep -c 'request_id=req-' "${TMP}/all.log")
if [ "${facts}" -ge 8 ]; then pass "request-fact lines present (${facts})"; else fail "too few request-fact lines (${facts})"; fi
grep 'request_id=req-' "${TMP}/all.log" | grep 'route=/v1/chat/completions' | grep 'status=200' | head -2 | sed 's/^/      /'
grep 'request_id=req-' "${TMP}/all.log" | grep 'status=401' | head -1 | sed 's/^/      /'
grep 'request_id=req-' "${TMP}/all.log" | grep 'client_disconnected' | head -1 | sed 's/^/      /'

echo
if [ "${fails}" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "${fails} CHECK(S) FAILED"; fi
exit "${fails}"
