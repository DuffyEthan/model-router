.PHONY: test test-cli test-plugin test-scripts test-installer test-evals release-checksums

SHA256 := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo shasum -a 256)
# The ref and checksum pinned in install.sh.
INSTALL_REF := $(shell sed -n 's/^MR_REF="\(.*\)"$$/\1/p' install.sh)
INSTALL_SHA := $(shell sed -n 's/^MR_ROUTE_PY_SHA256="\(.*\)"$$/\1/p' install.sh)

test: test-cli test-plugin test-scripts test-installer test-evals

test-cli:
	python3 -m unittest discover -s cli/tests -v

# scripts/router_report.py against synthetic transcripts. Stdlib only.
test-scripts:
	python3 -m unittest discover -s scripts/tests -v

# Needs the `claude` CLI (Claude Code 2.1.275+). Runs against the engine; no API
# calls and no login needed.
test-plugin:
	claude plugin validate plugin
	claude plugin test plugin

# Offline tests for the eval harness. The classifier is faked; no API calls.
test-evals:
	python3 -m unittest discover -s evals -p 'test_*.py' -v

# Lints install.sh, checks the pinned checksum matches the pinned ref in git,
# then runs the installer offline against a file:// mirror with a stub `claude`:
# good checksum installs as `model-router`, an old `route` link keeps working with a
# note, a shadowing `model-router` earlier on PATH is reported, bad checksum fails
# closed, no hash tool fails closed, and a cli/route.py in the current directory is
# never used.
# Needs the pinned ref in local git history (CI checks out with fetch-depth: 0).
test-installer:
	sh -n install.sh
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck -s sh install.sh; \
	else echo "shellcheck not found; skipping lint"; fi
	@set -eu; \
	ref='$(INSTALL_REF)'; want='$(INSTALL_SHA)'; inst='$(CURDIR)/install.sh'; \
	fail() { echo "FAIL: $$*" >&2; exit 1; }; \
	[ -n "$$ref" ] && [ -n "$$want" ] || fail "could not read MR_REF / MR_ROUTE_PY_SHA256 from install.sh"; \
	git cat-file -e "$$ref:cli/route.py" 2>/dev/null \
	  || fail "pinned ref $$ref is not in local git history (git fetch --tags / fetch-depth: 0)"; \
	got=$$(git show "$$ref:cli/route.py" | $(SHA256)); got=$${got%% *}; \
	[ "$$got" = "$$want" ] || fail "install.sh pins $$want but $$ref:cli/route.py hashes to $$got"; \
	echo "ok: pinned checksum matches $$ref:cli/route.py"; \
	t=$$(mktemp -d); trap 'rm -rf "$$t"' EXIT; \
	mkdir -p "$$t/mirror/$$ref/cli" "$$t/stub" "$$t/min" "$$t/run/cli" "$$t/home"; \
	git show "$$ref:cli/route.py" > "$$t/mirror/$$ref/cli/route.py"; \
	cp "$$t/mirror/$$ref/cli/route.py" "$$t/good.py"; \
	printf '#!/bin/sh\nexit 0\n' > "$$t/stub/claude"; chmod +x "$$t/stub/claude"; \
	echo 'print("local checkout copy; must not be installed")' > "$$t/run/cli/route.py"; \
	sh_bin=$$(command -v sh); \
	run() { (cd "$$t/run" && env HOME="$$t/home" PATH="$$1" MR_BASE_URL="file://$$t/mirror" \
	  "$$sh_bin" -s -- --cli-only < "$$inst") > "$$t/out" 2>&1; }; \
	installed="$$t/home/.local/share/model-router/route.py"; \
	run "$$t/stub:$$PATH" || { cat "$$t/out"; fail "install with good checksum failed"; }; \
	cmp -s "$$installed" "$$t/good.py" || fail "installed route.py is not the pinned, verified file"; \
	[ -L "$$t/home/.local/bin/model-router" ] || fail "model-router symlink not created"; \
	[ -e "$$t/home/.local/bin/route" ] && fail "a fresh install created the old route link"; \
	[ -x "$$installed" ] || fail "route.py not executable"; \
	echo "ok: curl|sh-style install fetches and verifies route.py, ignores ./cli/route.py"; \
	ln -s "$$installed" "$$t/home/.local/bin/route"; \
	run "$$t/stub:$$PATH" || { cat "$$t/out"; fail "reinstall over an old route link failed"; }; \
	grep -q "the command is now 'model-router'" "$$t/out" || { cat "$$t/out"; fail "no note about the old route link"; }; \
	[ "$$(readlink "$$t/home/.local/bin/route")" = "$$installed" ] || fail "old route link was changed"; \
	rm "$$t/home/.local/bin/route"; \
	echo "ok: an old route link is left working, with a deprecation note"; \
	mkdir -p "$$t/shadow"; cp "$$t/stub/claude" "$$t/shadow/model-router"; \
	run "$$t/shadow:$$t/home/.local/bin:$$t/stub:$$PATH" || { cat "$$t/out"; fail "install with a shadowed command failed"; }; \
	grep -q "another 'model-router' comes first" "$$t/out" || { cat "$$t/out"; fail "no note about a shadowed model-router"; }; \
	run "$$t/home/.local/bin:$$t/stub:$$PATH" || { cat "$$t/out"; fail "install with bin dir on PATH failed"; }; \
	grep -q "comes first\|not on your PATH" "$$t/out" && { cat "$$t/out"; fail "PATH note when model-router resolves correctly"; }; \
	echo "ok: warns when another model-router shadows the installed one, and only then"; \
	echo '# tampered' >> "$$t/mirror/$$ref/cli/route.py"; \
	if run "$$t/stub:$$PATH"; then cat "$$t/out"; fail "install with bad checksum succeeded"; fi; \
	grep -q 'checksum mismatch' "$$t/out" || { cat "$$t/out"; fail "no checksum mismatch message"; }; \
	cmp -s "$$installed" "$$t/good.py" || fail "bad download replaced the existing install"; \
	ls -a "$$t/home/.local/share/model-router" | grep -q '^\.route\.py\.' && fail "temp file left behind"; \
	echo "ok: bad checksum fails closed and leaves the existing install alone"; \
	cp "$$t/good.py" "$$t/mirror/$$ref/cli/route.py"; \
	for c in curl mkdir mktemp chmod mv rm ln python3; do \
	  p=$$(command -v "$$c") || fail "test needs $$c"; ln -s "$$p" "$$t/min/$$c"; done; \
	cp "$$t/stub/claude" "$$t/min/claude"; \
	if run "$$t/min"; then cat "$$t/out"; fail "install without sha256sum/shasum succeeded"; fi; \
	grep -q 'sha256sum or shasum is required' "$$t/out" || { cat "$$t/out"; fail "wrong error without hash tool"; }; \
	echo "ok: no hash tool fails closed"

# Print the install.sh pin block for a release tag. The tag must exist locally.
#   make release-checksums TAG=vX.Y.Z
release-checksums:
	@[ -n "$(TAG)" ] || { echo "usage: make release-checksums TAG=vX.Y.Z" >&2; exit 2; }
	@git rev-parse -q --verify "$(TAG)^{commit}" >/dev/null \
	  || { echo "error: $(TAG) not found; create it or run 'git fetch --tags'" >&2; exit 1; }
	@sum=$$(git show "$(TAG):cli/route.py" | $(SHA256)); \
	echo "# Paste into install.sh (pinned release block):"; \
	echo 'MR_REF="$(TAG)"'; \
	echo "MR_ROUTE_PY_SHA256=\"$${sum%% *}\""
