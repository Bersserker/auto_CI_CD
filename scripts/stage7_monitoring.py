"""Connect only our monitoring stack to stage 7 aggregate metrics."""

from scripts.stage6_monitoring import main

if __name__ == "__main__":
    main("scoring-stage7_default")
