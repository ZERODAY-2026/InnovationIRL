# OWID CO2 and energy subset

`owid_co2_subset.csv` is a subset of the Our World in Data "CO2 and Greenhouse Gas Emissions" dataset: 13 columns, years 2000 onwards, every country and aggregate. Empty cells are real gaps in the source and load as NULL.

- **Source:** https://github.com/owid/co2-data (`owid-co2-data.csv`, downloaded 18 Oct 2026)
- **Licence:** Creative Commons BY 4.0. Our World in Data notes that some underlying series carry the licences of their original providers, such as the Global Carbon Project and the Energy Institute; see the source repository.
- **Attribution:** Hannah Ritchie, Pablo Rosado and Max Roser (2023), "CO2 and Greenhouse Gas Emissions", published online at OurWorldInData.org.
- **Rebuild:** run `python scripts/owid_seed.py --download`. Then run the generated `snowflake/owid_seed.generated.sql` in Snowsight as `DATAMIND_ADMIN`.
