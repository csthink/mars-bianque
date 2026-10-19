#!/usr/bin/env bash
#
# Public content check.
#
# Covers content that is independent of any deployment environment but cannot be
# taken back once it is published, plus a path gate:
#   credential     quoted credential assignments (password, passwd, secret, token,
#                  apikey, api_key; case-insensitive)
#   private-key    PEM private key headers
#   cloud-key      AKIA, gh?_, github_pat_, glpat-, sk-ant-, xox?-, AIza shapes
#   local-path     /Users/<name>, /home/<name>
#   private-net    RFC 1918 IPv4 addresses (10/8, 172.16/12, 192.168/16)
#   path-gate      progress, review and local report files that must never be tracked:
#                  HANDOFF/, docs/tasks/, .demo/, any progress/ or reviews/ directory,
#                  .env and .env.* (except .env.example), playwright-report/,
#                  test-results/, *.tap
#
# The pre-commit hook and CI both run this one script, so the two cannot diverge.
# Patterns tied to a specific environment are not written here: a pattern would
# itself disclose what it is meant to block.
#
# Usage:
#   tools/check-public-safety.sh [--allowlist <file>] [--staged] [<dir>] [file...]
#   tools/check-public-safety.sh --selftest
#
#   <dir> defaults to the current directory. In a git repository only the index is
#   scanned: git ls-files lists the paths for the path gate and git grep --cached
#   reads the content, so tracked and staged files are covered while untracked,
#   ignored and worktree-only changes are not (CI uses this mode). When <dir> is
#   not a git repository, every regular file under it except .git/ is scanned.
#   With [file...] only those files are checked (relative to <dir> or absolute),
#   read from the worktree; with --staged they are read from the index, which is
#   what the pre-commit hook uses to check exactly what will be committed.
#   --allowlist defaults to public-safety-allowlist.txt next to this script; when
#   that file does not exist there are no rules. Each line is an extended regular
#   expression (bash [[ =~ ]] semantics) matched against "<relative path>:<line>";
#   a match lets that line through. Use it for synthetic test values only. The
#   allowlist does not apply to the path gate.
#
# Exit codes: 0 pass; 1 violations found or --selftest failed; 2 usage error.
#
# Before changing this script:
#   1. The source of truth is the git index, not a directory walk; nothing is
#      excluded by directory name. Ignored files never reach the repository, so
#      reporting them would be noise; tracked files are always scanned.
#   2. Patterns are always passed with -e and never through a word-split variable:
#      a pattern containing / | * would be glob-expanded by the shell and the scan
#      would silently match nothing while exiting 0.
#   3. Run --selftest after every change: "the real repository passes" cannot tell
#      "no violations" apart from "the scanner is broken".
#   4. Self-test samples are assembled at runtime with printf, so no complete
#      credential or path shape appears in this file and it does not flag itself.
#   5. Stay compatible with the bash 3.2 shipped with macOS: no mapfile, no
#      associative arrays; expand possibly empty arrays as ${a[@]+"${a[@]}"}.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$0")"
TITLE="Public content check"
DEFAULT_ALLOWLIST="$SCRIPT_DIR/public-safety-allowlist.txt"

CATEGORIES=(credential private-key cloud-key local-path private-net path-gate)

# Patterns.
# credential: the name may be followed by one quote (a JSON key), optional
# whitespace, then : or =, then a quoted value of at least 8 characters. Values
# starting with $ < { are placeholders ("${DB_PASSWORD}", "<fill in>",
# "{{ secret }}") and do not count.
PAT_CREDENTIAL='(password|passwd|secret|token|apikey|api_key)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$<{][^"'"'"']{7,}'
PAT_PRIVATE_KEY='BEGIN [A-Z ]*PRIVATE KEY'
PAT_CLOUD='AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}'
# local-path: must not follow a letter or digit, so "example.com/home/page" in a URL is
# not taken for a home directory.
PAT_LOCAL_PATH='(^|[^A-Za-z0-9_])/(Users|home)/[A-Za-z0-9._-]+'
PAT_PRIVATE_NET='(^|[^0-9.])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3})([^0-9.]|$)'
PAT_PATH_GATE='^HANDOFF/|^docs/tasks/|^\.demo/|(^|/)progress/|(^|/)reviews/|(^|/)\.env$|(^|/)\.env\.[^/]+$|(^|/)playwright-report/|(^|/)test-results/|\.tap$'
PAT_PATH_GATE_EXEMPT='(^|/)\.env\.example$'

usage() {
  cat >&2 <<EOF
Usage:
  $0 [--allowlist <file>] [--staged] [<dir>] [file...]
  $0 --selftest
EOF
}

# Reporting.
VIOLATIONS=0
ALLOWED_COUNT=0
HIT_COUNTS=()
for _c in "${CATEGORIES[@]}"; do HIT_COUNTS+=(0); done
if [ -t 1 ]; then RED=$'\033[31m'; RESET=$'\033[0m'; else RED=""; RESET=""; fi

bump() {
  local i
  for i in "${!CATEGORIES[@]}"; do
    if [ "${CATEGORIES[$i]}" = "$1" ]; then HIT_COUNTS[$i]=$(( HIT_COUNTS[$i] + 1 )); return; fi
  done
}

report() {  # category file line text (line and text are empty for the path gate)
  local category="$1" file="$2" line="$3" text="$4"
  if [ -n "$line" ]; then
    printf '%s[%s]%s %s:%s\n        %s\n' "$RED" "$category" "$RESET" "$file" "$line" "${text:0:160}"
  else
    printf '%s[%s]%s %s\n' "$RED" "$category" "$RESET" "$file"
  fi
  bump "$category"
  VIOLATIONS=$((VIOLATIONS + 1))
}

# Allowlist.
ALLOW_RULES=()
load_allowlist() {
  local f="$1" line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    ALLOW_RULES+=("$line")
  done < "$f"
}

allowed() {  # $1 = relative path:line content
  local r
  for r in ${ALLOW_RULES[@]+"${ALLOW_RULES[@]}"}; do
    [[ $1 =~ $r ]] && return 0
  done
  return 1
}

# Scanning.
MODE=""            # index | staged | worktree | fs
GATE_FILES=()      # paths checked by the path gate (relative to <dir>)
CONTENT_FILES=()   # files whose content is scanned in worktree and fs modes

handle_matches() {  # category grep-output
  local category="$1" matches="$2" m file rest line text
  [ -z "$matches" ] && return 0
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    file=${m%%:*}; rest=${m#*:}; line=${rest%%:*}; text=${rest#*:}
    if allowed "$file:$text"; then ALLOWED_COUNT=$((ALLOWED_COUNT + 1)); continue; fi
    report "$category" "$file" "$line" "$text"
  done <<< "$matches"
}

# Prints file:line:text for one pattern, reading content according to MODE.
# git grep runs with core.quotePath=false so non-ASCII paths are printed as-is
# and allowlist rules can match them.
grep_content() {  # pattern [extra grep options...]
  local pattern="$1"; shift
  case "$MODE" in
    index)
      git -c core.quotePath=false grep --cached -nIE --no-color "$@" -e "$pattern" 2>/dev/null || true ;;
    staged)
      git -c core.quotePath=false --literal-pathspecs grep --cached -nIE --no-color "$@" -e "$pattern" -- ${GATE_FILES[@]+"${GATE_FILES[@]}"} 2>/dev/null || true ;;
    *)
      [ "${#CONTENT_FILES[@]}" -eq 0 ] && return 0
      printf '%s\0' "${CONTENT_FILES[@]}" \
        | xargs -0 grep -HnIE --binary-files=without-match --color=never "$@" -e "$pattern" -- 2>/dev/null || true ;;
  esac
}

scan() {  # category pattern [extra grep options...]
  local category="$1" pattern="$2"; shift 2
  handle_matches "$category" "$(grep_content "$pattern" "$@")"
}

gate() {
  local f
  for f in ${GATE_FILES[@]+"${GATE_FILES[@]}"}; do
    [[ $f =~ $PAT_PATH_GATE_EXEMPT ]] && continue
    [[ $f =~ $PAT_PATH_GATE ]] && report "path-gate" "$f" "" ""
  done
  return 0
}

summary() {
  local parts="" i
  for i in "${!CATEGORIES[@]}"; do
    [ "${HIT_COUNTS[$i]}" -gt 0 ] && parts="$parts${parts:+, }${CATEGORIES[$i]} ${HIT_COUNTS[$i]}"
  done
  echo
  [ "$ALLOWED_COUNT" -gt 0 ] && echo "Allowlisted: $ALLOWED_COUNT line(s) (${ALLOWLIST})"
  if [ "$VIOLATIONS" -eq 0 ]; then
    echo "PASS: no violations found"
    return 0
  fi
  echo "FAIL: $VIOLATIONS violation(s) (${parts})"
  cat <<'EOF'
How to fix:
  - local-path, private-net: replace with a placeholder or an environment variable
  - path-gate: progress, review and local report files do not belong in this repository; move them out
  - credential, private-key, cloud-key: revoke the credential first, then rewrite history;
    deleting the file is not enough, the content stays in git history
  - synthetic test values: assemble them at runtime, or register the exact line in
    tools/public-safety-allowlist.txt with a reason
EOF
  return 1
}

# Self-test (positive and negative controls).
selftest() {
  local root failed=0 n=0 out rc hits rel dir
  local rc_untracked rc_tracked rc_staged rc_worktree
  # Inside a git hook, GIT_DIR and friends are set and would point the temporary
  # repositories' git commands at the real repository.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR
  root=$(mktemp -d "${TMPDIR:-/tmp}/public-safety-selftest.XXXXXX") || { echo "selftest: cannot create a temporary directory" >&2; return 2; }
  # The trap runs after the function returns, when locals are gone, so the
  # cleanup path lives in a global.
  SELFTEST_ROOT="$root"
  trap 'rm -rf "$SELFTEST_ROOT"' EXIT
  echo "==> ${TITLE}: selftest"

  # One positive sample per category, each scanned in its own directory: expect
  # exit code 1, this category's label, and no other category's label.
  positive() {  # category relative-file content
    local category="$1" rel="$2" body="$3" dir out rc other
    dir="$root/positive-$n"; n=$((n + 1))
    mkdir -p "$dir/$(dirname "$rel")"
    printf '%s\n' "$body" > "$dir/$rel"
    out=$(bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" 2>&1); rc=$?
    if [ "$rc" -ne 1 ] || ! printf '%s\n' "$out" | grep -qF "[$category]"; then
      echo "  x positive $category: not detected (exit $rc)"; failed=1; return
    fi
    for other in "${CATEGORIES[@]}"; do
      [ "$other" = "$category" ] && continue
      if printf '%s\n' "$out" | grep -qF "[$other]"; then
        echo "  x positive $category: also reported as $other"; failed=1; return
      fi
    done
    echo "  ok positive $category: detected"
  }

  positive "credential"  "config.yml" "$(printf 'db_password: "%s"' 'synthetic-value-0001')"
  positive "private-key" "key.pem"    "$(printf -- '-----BEGIN %s PRIVATE KEY-----' RSA)"
  positive "cloud-key"   "env.txt"    "$(printf 'AWS_KEY=AKIA%s' 'EXAMPLEEXAMPLE12')"
  positive "local-path"  "notes.md"   "$(printf 'see /Users/%s/notes.md' example)"
  positive "private-net" "db.conf"    "$(printf 'host = 10.%s' '20.30.40')"

  dir="$root/positive-gate"
  for rel in HANDOFF/a.md docs/tasks/b.md .demo/kind.yaml notes/progress/c.md docs/reviews/d.md \
             .env backend/.env.local out/playwright-report/index.html out/test-results/e.txt out/run.tap; do
    mkdir -p "$dir/$(dirname "$rel")"; printf 'clean\n' > "$dir/$rel"
  done
  out=$(bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" 2>&1); rc=$?
  hits=$(printf '%s\n' "$out" | grep -cF '[path-gate]')
  if [ "$rc" -eq 1 ] && [ "$hits" -eq 10 ]; then
    echo "  ok positive path-gate: 10 of 10 paths detected"
  else
    echo "  x positive path-gate: $hits of 10 paths detected (exit $rc)"; failed=1
  fi

  # Clean sample: placeholders, loopback, three-part versions, /home/ inside a
  # URL, home-relative paths, the generalized form recorded evidence uses, an
  # .env.example file and an evaluation report.
  dir="$root/negative"; mkdir -p "$dir/src" "$dir/eval/reports"
  cat > "$dir/src/notes.md" <<'EOF'
"token": "<paste-your-token>"
loopback 127.0.0.1, version 10.0.0, docs at https://example.com/home/page
the /home/ directory, ~/Library/Application Support/app/
pod address 10.244.x.x
EOF
  printf 'db_password: "%s"\n' '${DB_PASSWORD}' >> "$dir/src/notes.md"
  printf 'BIANQUE_LLM_API_KEY=\n' > "$dir/.env.example"
  printf '{}\n' > "$dir/eval/reports/report.json"
  out=$(bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then echo "  ok negative clean sample: passed"; else echo "  x negative clean sample: false positive (exit $rc)"; printf '%s\n' "$out" | sed 's/^/      /'; failed=1; fi

  # Index semantics: untracked is not reported; reported after git add; after the
  # worktree is cleaned, --staged still reads the index and reports, while file
  # mode reads the worktree and passes.
  dir="$root/git-mode"; mkdir -p "$dir"
  if git -C "$dir" init -q 2>/dev/null; then
    printf 'db_password: "%s"\n' 'synthetic-value-0002' > "$dir/probe.txt"
    bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" >/dev/null 2>&1; rc_untracked=$?
    git -C "$dir" add -f probe.txt
    bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" >/dev/null 2>&1; rc_tracked=$?
    printf 'clean\n' > "$dir/probe.txt"
    bash "$SCRIPT_PATH" --allowlist /dev/null --staged "$dir" probe.txt >/dev/null 2>&1; rc_staged=$?
    bash "$SCRIPT_PATH" --allowlist /dev/null "$dir" probe.txt >/dev/null 2>&1; rc_worktree=$?
    if [ "$rc_untracked" -eq 0 ] && [ "$rc_tracked" -eq 1 ] && [ "$rc_staged" -eq 1 ] && [ "$rc_worktree" -eq 0 ]; then
      echo "  ok index semantics: untracked ignored, staged reported, --staged reads the index, file mode reads the worktree"
    else
      echo "  x index semantics: untracked $rc_untracked (want 0), staged $rc_tracked (want 1), --staged $rc_staged (want 1), file mode $rc_worktree (want 0)"; failed=1
    fi
  else
    echo "  x index semantics: git init failed"; failed=1
  fi

  echo
  if [ "$failed" -eq 0 ]; then
    echo "PASS: selftest (${#CATEGORIES[@]} categories detected, clean sample passed, index semantics hold)"
    return 0
  fi
  echo "FAIL: selftest; the scanner is broken, do not trust its PASS until fixed"
  return 1
}

# Arguments.
ALLOWLIST="$DEFAULT_ALLOWLIST"; ALLOWLIST_EXPLICIT=0; STAGED=0; SELFTEST=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allowlist)
      [ "$#" -ge 2 ] || { echo "usage error: --allowlist needs an argument" >&2; usage; exit 2; }
      ALLOWLIST="$2"; ALLOWLIST_EXPLICIT=1; shift 2 ;;
    --staged) STAGED=1; shift ;;
    --selftest) SELFTEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "usage error: unknown option $1" >&2; usage; exit 2 ;;
    *) break ;;
  esac
done

if [ "$SELFTEST" -eq 1 ]; then
  selftest
  exit $?
fi

TARGET="${1:-.}"
[ "$#" -gt 0 ] && shift
[ -d "$TARGET" ] || { echo "usage error: no such directory: $TARGET" >&2; exit 2; }
if [ "$ALLOWLIST_EXPLICIT" -eq 1 ] && [ ! -r "$ALLOWLIST" ]; then
  echo "usage error: allowlist not readable: $ALLOWLIST" >&2; exit 2
fi
case "$ALLOWLIST" in /*) ;; *) ALLOWLIST="$PWD/$ALLOWLIST" ;; esac
[ -r "$ALLOWLIST" ] && load_allowlist "$ALLOWLIST"

cd "$TARGET" || { echo "usage error: cannot enter directory: $TARGET" >&2; exit 2; }
IN_GIT=0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 && IN_GIT=1
if [ "$STAGED" -eq 1 ] && [ "$IN_GIT" -eq 0 ]; then
  echo "usage error: --staged only works in a git repository: $TARGET" >&2; exit 2
fi

if [ "$#" -gt 0 ]; then
  for f in "$@"; do
    rel="$f"
    case "$rel" in
      /*)
        if [ "$rel" = "${rel#"$PWD"/}" ]; then
          echo "usage error: file is outside <dir>: $f" >&2; exit 2
        fi
        rel="${rel#"$PWD"/}" ;;
      ./*) rel="${rel#./}" ;;
    esac
    if [ "$STAGED" -eq 1 ]; then
      git --literal-pathspecs ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 \
        || { echo "usage error: not in the index: $f" >&2; exit 2; }
    else
      [ -f "$rel" ] || { echo "usage error: not an existing file: $f" >&2; exit 2; }
      CONTENT_FILES+=("$rel")
    fi
    GATE_FILES+=("$rel")
  done
  if [ "$STAGED" -eq 1 ]; then MODE=staged; MODE_DESC="${#GATE_FILES[@]} given file(s), index content"
  else MODE=worktree; MODE_DESC="${#GATE_FILES[@]} given file(s), worktree content"; fi
elif [ "$IN_GIT" -eq 1 ]; then
  MODE=index
  while IFS= read -r -d '' f; do GATE_FILES+=("$f"); done < <(git ls-files -z)
  MODE_DESC="git index, ${#GATE_FILES[@]} path(s)"
else
  MODE=fs
  while IFS= read -r -d '' f; do f="${f#./}"; GATE_FILES+=("$f"); CONTENT_FILES+=("$f"); done \
    < <(find . -type f -not -path './.git/*' -print0)
  MODE_DESC="not a git repository, ${#GATE_FILES[@]} regular file(s)"
fi

echo "==> ${TITLE}: ${TARGET} (${MODE_DESC})"
gate
scan "credential" "$PAT_CREDENTIAL" -i
scan "private-key" "$PAT_PRIVATE_KEY"
scan "cloud-key" "$PAT_CLOUD"
scan "local-path" "$PAT_LOCAL_PATH"
scan "private-net" "$PAT_PRIVATE_NET"
summary
