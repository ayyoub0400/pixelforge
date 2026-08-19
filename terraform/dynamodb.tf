resource "aws_dynamodb_table" "jobs" {

	name = "${var.project_name}-${var.environment}-jobs"

  
  billing_mode = "PAY_PER_REQUEST"	

  hash_key = "job_id"

  attribute {

    name = "job_id"
    type = "S"

  }

  point_in_time_recovery {

    enabled = false
  
  }
  
}
