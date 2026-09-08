"""Test script for the Thalian Hall extractor implementation."""
import os
import sys

# Ensure the root project directory is on the system path for seamless module resolution
sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scrapers.thalian_hall.run_extractor import ThalianHallExtractor  # noqa: E402
from utils.logger import setup_logger  # noqa: E402

logger = setup_logger("test_thalian_hall", log_to_file=False)


def test_thalian_hall_pipeline():
    """Executes a framework validation run against the Thalian Hall extractor."""
    logger.info(" Starting Thalian Hall Pipeline Test Run")

    # Initialize using the framework configuration parameters
    extractor = ThalianHallExtractor(
        local_test=True,  # Restricts processing to a smaller subset of shows
        show_count=5,  # Limits processing to 5 shows for rapid end-to-end iteration
        save_csv_locally=True,  # Saves a verification file directly to the data/ folder
        csv_incremental_mode=False,
    )

    # Run the core pipeline lifecycle (Extract -> Save Raw -> Parse -> Validate Schema -> Save CSV)
    result = extractor.run()
    logger.info(f" Pipeline Test Completed. Result Summary: {result}")


if __name__ == "__main__":
    test_thalian_hall_pipeline()
