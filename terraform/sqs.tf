resource "aws_sqs_queue" "jobs_dlq" {
  name = "${var.project_name}-${var.environment}-jobs-dlq"

  #14 days
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "jobs" {
  name = "${var.project_name}-${var.environment}-jobs"

  #how long til message is visible in queue again
  visibility_timeout_seconds = 60

  #how long polling session is
  receive_wait_time_seconds = 20

  message_retention_seconds = 345600

  #messages sent to DLQ
  redrive_policy = jsonencode({
    #dlq destination
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    #how many fails before sent off
    maxReceiveCount = 3

  })
}

#who can send messages to dlq
resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id

  #the rules
  redrive_allow_policy = jsonencode({
    #type of resource we allow
    redrivePolicy = "byQueue"

    #the queue we are allowing dead messages from
    sourceQueueArns = [aws_sqs_queue.jobs.arn]

  })

}

