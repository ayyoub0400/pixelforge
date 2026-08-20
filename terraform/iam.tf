#policy to allow resources to assume roles within this account
data "aws_iam_policy_document" "assume_from_account" {

  statement {

    actions = ["sts:AssumeRole"]


    principals {

      type        = "AWS"
      identifiers = [data.aws_caller_identity.current.arn]

    }

  }

}



#this is what the api and workers can do

data "aws_iam_policy_document" "api" {

  #our rules
  statement {

    #statement id
    sid = "WriteUploads"

    #what we want api to do
    actions = ["s3:putObject"]

    #onto what
    resources = ["${aws_s3_bucket.media.arn}/uploads/*"]
  }

  statement {

    sid = "EnqueueJobs"

    actions = ["sqs:SendMessage"]

    resources = [aws_sqs_queue.jobs.arn]

  }

  statement {

    sid = "JobRecords"

    actions = ["dynamoDB:putItem", "dynamoDB:getItem"]

    resources = [aws_dynamodb_table.jobs.arn]

  }

  statement {

    sid = "ReadinessCheck"

    actions = ["s3:ListBucket", "sqs:GetQueueAttributes"]

    resources = [aws_s3_bucket.media.arn, aws_sqs_queue.jobs.arn]

  }


}

data "aws_iam_policy_document" "worker" {

  #our rules
  statement {

    #statement id
    sid = "WriteUploads"

    #what we want api to do
    actions = ["s3:putObject"]

    #onto what
    resources = ["${aws_s3_bucket.media.arn}/outputs/*"]
  }

  statement {

    sid = "ReadUploads"

    actions = ["s3:ListObject"]

    resources = ["${aws_s3_bucket.media.arn}/uploads/*"]
  }


  statement {

    sid = "ConsumeQueue"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {

    sid       = "UpdateJobStatus"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  statement {

    sid = "JobRecords"

    actions = ["dynamoDB:putItem", "dynamoDB:getItem"]

    resources = [aws_dynamodb_table.jobs.arn]
  }
  statement {

    sid = "ReadinessCheck"

    actions = ["s3:ListBucket"]

    resources = [aws_s3_bucket.media.arn]

  }
}



resource "aws_iam_role" "api" {

  name               = "${var.project_name}-${var.environment}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_from_account.json

}

#the role and what it can assume
resource "aws_iam_role" "worker" {

  name               = "${var.project_name}-${var.environment}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_from_account.json

}

#what our created role can do --- role + policy ---
resource "aws_iam_role_policy" "api" {

  name   = "${var.project_name}-${var.environment}-api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json

}

resource "aws_iam_role_policy" "worker" {

  name   = "${var.project_name}-${var.environment}-worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json

}


