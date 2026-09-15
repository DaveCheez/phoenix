from base.env import config


AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME", default="phoenixvanz")
AWS_S3_ENDPOINT_URL = config(
    "AWS_S3_ENDPOINT_URL",
    default="https://fra1.digitaloceanspaces.com",
)
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="fra1")
AWS_S3_CUSTOM_DOMAIN = config("AWS_S3_CUSTOM_DOMAIN", default=None)
AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "max-age=86400"}
AWS_DEFAULT_ACL = "public-read"
AWS_QUERYSTRING_AUTH = False
AWS_S3_FILE_OVERWRITE = False

# Static assets are served by WhiteNoise. Only user-uploaded media uses Spaces.
DEFAULT_FILE_STORAGE = "cdn.backends.MediaRootS3BotoStorage"
