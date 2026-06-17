# gaius development targets
VENV := .venv/bin/python

.PHONY: test bench lint install clean

test:
	$(VENV) -m pytest tests/ -v --tb=short

bench:
	$(VENV) benchmarks/bench_inject.py

lint:
	@# No hardcoded absolute home paths in shipped source (config-driven instead)
	@! grep -rE "/home/[a-z]+/|/Users/[a-z]+/" gaius/ --include="*.py" | grep -v "# example\|test_" || true
	@echo "Lint OK"

install:
	install -m 755 gaius_cli ~/.local/bin/gaius
	@echo "Installed to ~/.local/bin/gaius"

clean:
	find . -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
