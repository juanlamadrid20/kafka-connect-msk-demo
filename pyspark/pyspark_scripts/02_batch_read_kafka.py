# Purpose: Batch read Kafka topic and summarize by country and by total sales,
#          to console and Amazon S3 as CSV
# Author:  Gary A. Stafford
# Date: 2021-09-04

import os

import boto3
import pyspark.sql.functions as F
from ec2_metadata import ec2_metadata
from pyspark.sql import SparkSession
from pyspark.sql.types import StructField, StructType, IntegerType, \
    StringType, FloatType, TimestampType
from pyspark.sql.window import Window

topic_input = "pagila.sales.spark.streaming.55"
csv_output = "sales_by_country.csv"

os.environ['AWS_DEFAULT_REGION'] = ec2_metadata.region
ssm_client = boto3.client("ssm")


def main():
    params = get_parameters()

    spark = SparkSession \
        .builder \
        .appName("kafka-batch-sales") \
        .getOrCreate()

    # spark.sparkContext.setLogLevel("DEBUG")

    df_sales = read_from_kafka(spark, params)

    summarize_sales(params, df_sales)


def read_from_kafka(spark, params):
    options_read = {
        "kafka.bootstrap.servers":
            params["kafka_servers"],
        "subscribe":
            topic_input,
        "startingOffsets":
            "earliest",
        "endingOffsets":
            "latest",
        "kafka.ssl.truststore.location":
            "/tmp/kafka.client.truststore.jks",
        "kafka.security.protocol":
            "SASL_SSL",
        "kafka.sasl.mechanism":
            "AWS_MSK_IAM",
        "kafka.sasl.jaas.config":
            "software.amazon.msk.auth.iam.IAMLoginModule required;",
        "kafka.sasl.client.callback.handler.class":
            "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
    }

    df_sales = spark.read \
        .format("kafka") \
        .options(**options_read) \
        .load()

    return df_sales


def summarize_sales(params, df_sales):
    schema = StructType([
        StructField("payment_id", IntegerType(), False),
        StructField("customer_id", IntegerType(), False),
        StructField("amount", FloatType(), False),
        StructField("payment_date", TimestampType(), False),
        StructField("city", StringType(), True),
        StructField("district", StringType(), True),
        StructField("country", StringType(), False),
    ])

    window_spec = Window.partitionBy("country").orderBy("amount")
    window_spec_agg = Window.partitionBy("country")

    df_output = df_sales \
        .selectExpr("CAST(value AS STRING)") \
        .select(F.from_json("value", schema=schema).alias("data")) \
        .select("data.*") \
        .withColumn("row",
                    F.row_number().over(window_spec)) \
        .withColumn("orders",
                    F.count(F.col("amount")).over(window_spec_agg)) \
        .withColumn("sales",
                    F.sum(F.col("amount")).over(window_spec_agg)) \
        .where(F.col("row") == 1) \
        .orderBy("sales", ascending=False) \
        .select("country", (F.format_number(F.col("sales"), 2)).alias("sales"), "orders") \
        .coalesce(1)

    df_output \
        .write \
        .format("console") \
        .option("numRows", 25) \
        .option("truncate", False) \
        .save()

    df_output \
        .write \
        .csv(path=f"s3a://{params['kafka_demo_bucket']}/spark_output/{csv_output}",
             header=True, sep="|") \
        .mode("overwrite")


def get_parameters():
    """Load parameter values from AWS Systems Manager (SSM) Parameter Store"""

    params = {
        "kafka_servers": ssm_client.get_parameter(
            Name="/kafka_spark_demo/kafka_servers")["Parameter"]["Value"],
        "kafka_demo_bucket": ssm_client.get_parameter(
            Name="/kafka_spark_demo/kafka_demo_bucket")["Parameter"]["Value"],
    }

    return params


if __name__ == "__main__":
    main()