import base64
import time
import datetime
import re
import os
import json
from google.cloud import bigquery
from google.cloud import documentai_v1beta3 as documentai
from google.cloud import storage
from google.cloud import pubsub_v1

 
#Reading environment variables
gcs_output_uri_prefix = os.environ.get('GCS_OUTPUT_URI_PREFIX')
project_id = os.environ.get('GCP_PROJECT')
location = os.environ.get('PARSER_LOCATION')
processor_id = os.environ.get('PROCESSOR_ID')
timeout = int(os.environ.get('TIMEOUT'))
 
# An array of Future objects: environvery call to publish() returns an instance of Future
geocode_futures = []
# Setting variables
gcs_output_uri = f"gs://{project_id}-output-insurance"
gcs_archive_bucket_name = f"{project_id}-archived-insurance"
destination_uri = f"{gcs_output_uri}/{gcs_output_uri_prefix}/"
name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"
dataset_name = 'galaxy_insurance'
table_name = 'claim_form'
gcs_review_uri = f"{project_id}-pending-insurance"

# Create a dict to create the schema and to avoid BigQuery load job fails due to inknown fields
bq_schema={
    "claim_no":"STRING",  
    "admission_no":"STRING",
    "age":"INTEGER", 
    "hospitalization_duration":"INTEGER",
    "date_of_admission":"TIMESTAMP"
}
bq_load_schema=[]
for key,value in bq_schema.items():
    bq_load_schema.append(bigquery.SchemaField(key,value))
 
docai_client = documentai.DocumentProcessorServiceClient()
storage_client = storage.Client()
bq_client = bigquery.Client()
pub_client = pubsub_v1.PublisherClient()
 
def write_to_bq(dataset_name, table_name, entities_extracted_dict):
 
    dataset_ref = bq_client.dataset(dataset_name)
    table_ref = dataset_ref.table(table_name)

    test_dict=entities_extracted_dict.copy()
    for key,value in test_dict.items():
      if key not in bq_schema:
          print ('Deleting key:' + key)
          del entities_extracted_dict[key]

    row_to_insert =[]
    row_to_insert.append(entities_extracted_dict)
 
    json_data = json.dumps(row_to_insert, sort_keys=False)
    print(json_data)
    #Convert to a JSON Object
    json_object = json.loads(json_data)
   
    job_config = bigquery.LoadJobConfig(schema=bq_load_schema)
    job_config.source_format = bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
 
    job = bq_client.load_table_from_json(json_object, table_ref, job_config=job_config)
    error = job.result()  # Waits for table load to complete.
    print(error)

def get_text(doc_element: dict, document: dict):
    # Document AI identifies form fields by their offsets in document text. This function converts offsets to text snippets.
    response = ''
    # If a text segment spans several lines, it will be stored in different text segments.
    for segment in doc_element.text_anchor.text_segments:
        start_index = (
            int(segment.start_index)
            if segment in doc_element.text_anchor.text_segments
            else 0
        )
        end_index = int(segment.end_index)
        response += document.text[start_index:end_index]
    return response
 
def insurance(event, context):
    gcs_input_uri = 'gs://' + event['bucket'] + '/' + event['name']
    print('Printing the contentType: ' + event['contentType'])

    if(event['contentType'] == 'image/gif' or event['contentType'] == 'application/pdf' or event['contentType'] == 'image/tiff' ):
        input_config = documentai.types.document_processor_service.BatchProcessRequest.BatchInputConfig(gcs_source=gcs_input_uri, mime_type=event['contentType'])
        # Where to write results
        output_config = documentai.types.document_processor_service.BatchProcessRequest.BatchOutputConfig(gcs_destination=destination_uri)
 
        request = documentai.types.document_processor_service.BatchProcessRequest(
            name=name,
            input_configs=[input_config],
            output_config=output_config,
        )
 
        operation = docai_client.batch_process_documents(request)
 
        # Wait for the operation to finish
        operation.result(timeout=timeout)
 
        match = re.match(r"gs://([^/]+)/(.+)", destination_uri)
        output_bucket = match.group(1)
        prefix = match.group(2)
      
        #Get a pointer to the GCS bucket where the output will be placed
        bucket = storage_client.get_bucket(output_bucket)
      
        blob_list = list(bucket.list_blobs(prefix=prefix))
        print('Output files:')
 
        for i, blob in enumerate(blob_list):
            # Download the contents of this blob as a bytes object.
            if '.json' not in blob.name:
                print('blob name ' + blob.name)
                print(f"skipping non-supported file type {blob.name}")
            else:
                #Setting the output file name based on the input file name
                print('Fetching from ' + blob.name)
                start = blob.name.rfind("/") + 1
                end = blob.name.rfind(".") + 1           
                input_filename = blob.name[start:end:] + 'gif'
                print('input_filename ' + input_filename)
      
                # Getting ready to read the output of the parsed document - setting up "document"
                blob_as_bytes = blob.download_as_bytes()
                document = documentai.types.Document.from_json(blob_as_bytes)
      
                #Reading all entities into a dictionary to write into a BQ table
                entities_extracted_dict = {}
                entities_extracted_dict['input_file_name'] = input_filename
                for page in document.pages:
                    for form_field in page.form_fields:  
                        field_name = get_text(form_field.field_name,document)
                        field_value = get_text(form_field.field_value,document)
                        if 'date' in field_name.strip().lower() :
                            entities_extracted_dict['date_of_admission'] = datetime.datetime.strptime( field_value.strip(),"%d/%m/%Y" ).timestamp()                        
                        if 'hospitalization duration' in field_name.strip().lower() :
                            entities_extracted_dict['hospitalization_duration'] = int(field_value)
                        if field_name.strip().lower() == 'hospitalization duration:' :
                            entities_extracted_dict['hospitalization_duration'] = int(field_value)
                        if 'age' in field_name.strip().lower() :
                            entities_extracted_dict['age'] = int(field_value)
                        if 'admission no' in field_name.strip().lower() :
                            entities_extracted_dict['admission_no'] = field_value.strip()
                        if 'claim no' in field_name.strip().lower() :
                            entities_extracted_dict['claim_no'] = field_value.strip()
                test_dict=entities_extracted_dict.copy()
                for key,value in test_dict.items():
                    if key not in bq_schema:
                        print ('Deleting key:' + key)
                        del entities_extracted_dict[key]
                if(len(entities_extracted_dict)<5):
                    print(entities_extracted_dict)
                    print(len(entities_extracted_dict))
                    #Deleting the intermediate files created by the Doc AI Parser
                    blobs = bucket.list_blobs(prefix=gcs_output_uri_prefix)                    
                    for blob1 in blobs:
                        blob1.delete()                     
                    source_bucket = storage_client.bucket(event['bucket'])
                    source_blob1 = source_bucket.blob(event['name'])
                    destination_bucket = storage_client.bucket(gcs_review_uri)
                    print(destination_bucket)
                    blob_copy2 = source_bucket.copy_blob(source_blob1, destination_bucket, event['name'])
                    #delete from the input folder
                    source_blob1.delete()
                else:
                    print(entities_extracted_dict)
                    print('Writing to BQ')
                    #Write the entities to BQ
                    write_to_bq(dataset_name, table_name, entities_extracted_dict)
                    #print(blobs)
                    #Deleting the intermediate files created by the Doc AI Parser
                    blobs = bucket.list_blobs(prefix=gcs_output_uri_prefix)
                    for blob in blobs:
                        blob.delete()
                    #Copy input file to archive bucket
                    source_bucket = storage_client.bucket(event['bucket'])
                    source_blob = source_bucket.blob(event['name'])
                    destination_bucket = storage_client.bucket(gcs_archive_bucket_name)
                    blob_copy = source_bucket.copy_blob(source_blob, destination_bucket, event['name'])
                    #delete from the input folder
                    source_blob.delete()
                
        
    else:
        print('Cannot parse the file type')


if __name__ == '__main__':
    print('Calling from main')
    testEvent={"bucket":project_id+"input-insurance", "contentType": "application/pdf", "name":"insurance2.pdf"}
    testContext='test'
    insurance(testEvent,testContext)
