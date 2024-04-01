import gzip
import io
import os
import requests
import dlt
from datetime import datetime, timedelta
from google.cloud import storage
import pandas as pd
import json
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
import numpy as np

os.environ['FLIGHTS_DEPARTURES__DESTINATION__FILESYSTEM__BUCKET_URL'] = f'gs://de-project-flight-analyzer'

def fetch_csv(iata=None):

    API_ACCESS_KEY = os.environ.get(f'API_{iata}_ACCESS_KEY')
    if not API_ACCESS_KEY:
        raise ValueError(f'API_{iata}_ACCESS_KEY not defined')
    url_base = f"http://api.aviationstack.com/v1/flights?access_key={API_ACCESS_KEY}&dep_iata={iata.upper()}"
    offset = 0
    output_file = []

    while True:
        url = f"{url_base}&offset={offset}"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        temp_json = data.get('data', [])
        output_file.extend(temp_json)
        if int(data["pagination"]["count"]) < 100:
            break
        offset += 100
    return output_file

def api_to_gcs(ds=None, iata=None):
    ds_datetime = datetime.strptime(ds, '%Y-%m-%d')
    yesterday = (ds_datetime - timedelta(days=1)).strftime('%Y_%m_%d')
    pipeline = dlt.pipeline(
        pipeline_name='flights_departures',
        destination='filesystem',
        dataset_name=f'{iata}'
    )
    json_file = fetch_csv(iata=iata)
    if json_file:
        load_info = pipeline.run(
            json_file, 
            table_name=f"{iata}_{yesterday}", 
            write_disposition="replace"
            )
        print(load_info)
    else:
        print("No data to upload.")

def transform_data(json_data=None, yesterday=None):
    df = pd.json_normalize(json_data)
    yesterday = yesterday.strftime('%Y-%m-%d')
    old_columns = [
        'flight_date',
        'flight__number',
        'flight__iata',
        'departure__airport',
        'departure__iata',
        'departure__scheduled',
        'departure__actual',
        'departure__delay',
        'arrival__airport',
        'arrival__iata',
        'arrival__timezone',
        'arrival__scheduled',  
        'arrival__actual',
        'arrival__delay',
        'airline__name',
        'airline__iata',
    ]
    new_columns = [column.replace('__', '_') for column in old_columns]
    
    # Select the desired columns first
    df_old = df[old_columns]
    # Apply the filter for 'yesterday' on the 'departure__scheduled' column
    df_filtered = df_old[df_old['flight_date'] == yesterday]
    # Rename columns by replacing double underscores with single underscores
    df_filtered.columns = new_columns
    df_filtered.replace({np.nan: None}, inplace=True)
    # Convert the filtered and renamed DataFrame to a dictionary
    json_file = df_filtered.to_dict(orient='records')  # Assuming you want a list of records
    for json_line in json_file:
        # Assuming json_line is a dictionary representing your data
        json_line['flight_number'] = int(json_line.get('flight_number', 0))
        json_line['departure_delay'] = float(json_line.get('departure_delay', 0.0))
        json_line['arrival_delay'] = float(json_line.get('arrival_delay', 0.0))
        # Repeat for any other INT64 fields
        yield json_line

def gcs_to_bigquery(ds=None, iata=None):
    # Define your GCS parameters
    ds_minus_one = datetime.strptime(ds, '%Y-%m-%d') - timedelta(days=1)
    yesterday = ds_minus_one.strftime('%Y_%m_%d')
    bucket_name = 'de-project-flight-analyzer'
    json_file_path = f'{iata}/{iata}_{yesterday}/'

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=json_file_path)

    all_data = []

    for blob in blobs:
        # Download the blob as bytes
        bytes_data = blob.download_as_bytes()
        with gzip.open(io.BytesIO(bytes_data), 'rt', encoding='utf-8') as gzip_file:
            for line in gzip_file:
                data = json.loads(line)
                # Perform your data transformation here
                all_data.append(data)
        break
    else:  # No files found
        raise FileNotFoundError(f"No files found for prefix {json_file_path}")
    
    json_to_bq = transform_data(
        json_data=all_data, 
        yesterday=ds_minus_one
        )

    bigquery_client = bigquery.Client()
    dataset_id = f'{bucket_name}.cities_raw_data'
    table_id = f'{iata}'
    full_table_id = f"{dataset_id}.{table_id}"
    schema = [
        bigquery.SchemaField("flight_date", "DATE"),
        bigquery.SchemaField("flight_number", "INT64"),
        bigquery.SchemaField("flight_iata", "STRING"),
        bigquery.SchemaField("departure_airport", "STRING"),
        bigquery.SchemaField("departure_iata", "STRING"),
        bigquery.SchemaField("departure_scheduled", "TIMESTAMP"),
        bigquery.SchemaField("departure_actual", "TIMESTAMP"),
        bigquery.SchemaField("departure_delay", "FLOAT64"),
        bigquery.SchemaField("arrival_airport", "STRING"),
        bigquery.SchemaField("arrival_iata", "STRING"),
        bigquery.SchemaField("arrival_timezone", "STRING"),
        bigquery.SchemaField("arrival_scheduled", "TIMESTAMP"),
        bigquery.SchemaField("arrival_actual", "TIMESTAMP"),
        bigquery.SchemaField("arrival_delay", "FLOAT64"),
        bigquery.SchemaField("airline_name", "STRING"),
        bigquery.SchemaField("airline_iata", "STRING")
    ]
    table = bigquery.Table(f"{dataset_id}.{table_id}", schema=schema)

    # Try to fetch the table, and create it if it doesn't exist
    try:
        bigquery_client.get_table(full_table_id)  # This checks if the table exists
        print(f"Table {full_table_id} already exists.")
    except NotFound:
        # Define the schema as before
        schema = [
            # Your schema definition here
        ]
        table = bigquery.Table(full_table_id, schema=schema)
        bigquery_client.create_table(table)  # This creates the table
        print(f"Table {full_table_id} created.")
    errors = bigquery_client.insert_rows_json(f"{dataset_id}.{table_id}", json_to_bq)
    if errors == []:
        print("New rows have been added.")
    else:
        print("Encountered errors while inserting rows: {}".format(errors))

    # pipeline = dlt.pipeline(
    #     pipeline_name='upload_to_bq',
    #     destination='bigquery',
    #     dataset_name='cities_raw_data'
    # )

    # if json_to_bq:
    #     load_info = pipeline.run(
    #         json_to_bq, 
    #         table_name=f"{iata}",
    #         write_disposition="append"
    #     )
    #     print(load_info)
    # else:
    #     print("No data to upload.")

def raw_to_datamart(ds=None, iata=None):
    # Initialize a BigQuery client
    client = bigquery.Client()

    # Define your SQL query for data transformation
    # This is a simple example that creates a new table with transformed data
    # Replace this with your actual data transformation query
    query = """
        CREATE OR REPLACE TABLE `project.dataset.new_table` AS
        SELECT 
            column1, 
            column2,
            column1 * column2 AS column3  # An example transformation
        FROM 
            `project.dataset.original_table`
    """

    # Run the query
    query_job = client.query(query)

    # Wait for the query to finish
    query_job.result()

    print("Query completed. The data has been transformed and stored in project.dataset.new_table.")
    