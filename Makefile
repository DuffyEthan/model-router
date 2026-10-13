.PHONY: test test-cli test-plugin

test: test-cli test-plugin

test-cli:
	python3 -m unittest discover -s cli/tests -v

# Needs the `claude` CLI (Claude Code 2.1.275+). Runs against the engine; no API calls.
test-plugin:
	claude plugin validate plugin
	claude plugin test plugin
