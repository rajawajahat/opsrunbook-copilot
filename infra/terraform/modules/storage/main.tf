data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.env}"
  bucket_name = "${local.name_prefix}-evidence-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

resource "aws_s3_bucket" "evidence" {
  bucket = local.bucket_name
}


resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Optional lifecycle to expire old evidence (useful in dev and to control costs)
resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  count  = var.evidence_retention_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-evidence"
    status = "Enabled"
    expiration { days = var.evidence_retention_days }
  }
}

# --- DynamoDB tables ---
resource "aws_dynamodb_table" "incidents" {
  name         = "${local.name_prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

}

resource "aws_dynamodb_table" "snapshots" {
  name         = "${local.name_prefix}-snapshots"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

resource "aws_dynamodb_table" "packets" {
  name         = "${local.name_prefix}-packets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}
