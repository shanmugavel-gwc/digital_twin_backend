"""Editable engineering assumptions that seed the reference tables (docs/ASSUMPTIONS.md A-01..A-06).
Nothing here is measured; every entry is an assumption surfaced in the UI."""
PLANT_NAMES = {"IN-TN-01": "Chennai Electronics Plant", "IN-KA-01": "Bengaluru Electronics Plant"}

# A-01: line -> plant. Verified against fact_hourly_production at load time (fails loudly on contradiction).
LINE_PLANT = {"SMT-L1": "IN-TN-01", "FAL-L1": "IN-TN-01", "SMT-L2": "IN-KA-01", "FAL-L2": "IN-KA-01"}
LINE_FAMILY = {"SMT-L1": "SMT", "SMT-L2": "SMT", "FAL-L1": "FAL", "FAL-L2": "FAL"}

# A-02: routing has no line_id. SMT stations -> the plant's SMT line; every other area -> the plant's FAL line.
# Each station therefore maps to exactly ONE line PER PLANT (two rows in total, one per plant).
AREA_TO_FAMILY = {"SMT": "SMT", "FINAL_ASSEMBLY": "FAL", "TEST": "FAL", "QUALITY": "FAL",
                  "PACKAGING": "FAL", "WAREHOUSE": "FAL"}

# A-03: machine -> station by machine_type. Assembly Cell has no unambiguous station: ST-FAL-010 assumed.
MACHINE_TYPE_STATION = {
    "Pick and Place": "ST-SMT-040", "Reflow Oven": "ST-SMT-050", "AOI": "ST-SMT-060", "SPI": "ST-SMT-030",
    "ICT Tester": "ST-SMT-080", "Assembly Cell": "ST-FAL-010", "RF Tester": "ST-TST-020",
    "Camera Tester": "ST-TST-030", "Aging Chamber": "ST-TST-050", "Leak Tester": "ST-FAL-050",
}

# A-04: inter-station buffers (units). Engineering assumption, not measured.
DEFAULT_BUFFER_UNITS = 20

# Expected source row counts (acceptance check)
EXPECTED_ROWS = {"dim_machine": 15, "dim_station": 21, "dim_product_routing": 63,
                 "fact_digital_twin_scenario": 300, "fact_hourly_production": 8832}
