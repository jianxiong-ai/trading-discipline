.PHONY: init test once up logs down

init:
	cp -n config.example.yaml config.yaml || true
	cp -n .env.example .env || true

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

once:
	PYTHONPATH=src python3 -m astock_bot.main once --node 10:15 --dry-run

up:
	docker compose up -d --build

logs:
	docker compose logs -f monitor

down:
	docker compose down

