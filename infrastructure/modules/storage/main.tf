resource "yandex_storage_bucket" "this" {
  folder_id     = var.folder_id
  bucket        = var.bucket_name
  force_destroy = false
  versioning { enabled = true }
  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
  lifecycle_rule {
    id                                     = "abort-incomplete-uploads"
    enabled                                = true
    abort_incomplete_multipart_upload_days = 7
  }
}

resource "yandex_storage_bucket_policy" "tls" {
  bucket = yandex_storage_bucket.this.bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = ["arn:aws:s3:::${var.bucket_name}", "arn:aws:s3:::${var.bucket_name}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
