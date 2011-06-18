.PHONY: pyflakes clean test
pyflakes:
	find . -name '*.py' -print0 | xargs -0 pyflakes

clean:
	find . -name '*.py[co]' -delete
	rm -rf tmp_* current

test:
	@testify -v tests.test
