.PHONY: default pyflakes clean test production docs

default: docs

pyflakes:
	find zygote tests -name '*.py' -print0 | xargs -0 pyflakes

clean:
	find . -name '*.py[co]' -delete
	rm -rf tmp_* current

test:
	@testify -v tests

production:

docs:
	make -C docs html
