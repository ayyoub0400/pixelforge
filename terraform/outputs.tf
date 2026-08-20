output "s3_bucket" {
	description = "s3 bucket name" 
	value = aws_s3_bucket.media.id
}

output "sqs_queue_url" {
	description = "SQS Queue URL for env variable"
	value = aws_sqs_queue.jobs.url
}

output "sqs_queue_arn" {
	description = "sqs queue arn for iam policies and cloudwatch alarms"
	value = aws_sqs_queue.jobs.arn
}

output "sqs_dlq_url" {
	description = "sqs dlq url"
	value = aws_sqs_queue.jobs_dlq.url
}

output "dynamodb_table" {
	description = "dynamodb table name"
	value = aws_dynamodb_table.jobs.name
}

output "dynamodb_table_arn" {
	description = "dynamodb table arn"
	value = aws_dynamodb_table.jobs.arn
}

output "api_role_arn" {
	description = "role assumed by api, used in IRSA"
	value = aws_iam_role.api.arn
}

output "worker_role_arn" {
	description = "role assumed by worker"
	value = aws_iam_role.worker.arn
}

output "ecr_api_url" {
	description = "api container repo"
	value = aws_ecr_repository.api.repository_url
}

output "ecr_worker_url" {
	description = "worker container repo"
	value = aws_ecr_repository.worker.repository_url
}

