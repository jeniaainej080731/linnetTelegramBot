run:
	python main.py

fmt:
	python -m pip install ruff
	ruff format .

lint:
	python -m pip install ruff
	ruff check .
