.PHONY: pyflakes
pyflakes:
	find . -name '*.py' -print0 | xargs -0 pyflakes
