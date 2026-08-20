#the bucket and its config options

resource "aws_s3_bucket" "media" {
  #bucket names need to be globally unique
  bucket = "${var.project_name}-${var.environment}-media-${data.aws_caller_identity.current.account_id}"

  #allows tf to destroy a bucket even if objects exist in
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "media" {

  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {

  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {

  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      #AES256 = SSE-S3
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "media" {

  bucket = aws_s3_bucket.media.id

  versioning_configuration {
    status = "Disabled"
  }

}
