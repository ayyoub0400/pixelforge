resource "aws_ecr_repository" "api" {

	name = "${var.project_name}/api"

  #immutable so the tag cannot be overwritten
  #stops someone from overwriting the production image
  #good for security too

  image_tag_mutability = "IMMUTABLE"

  #free cve scan
  image_scanning_configuration {

    scan_on_push = true

  }

  #disregards any existing images
  force_delete = true

}

resource "aws_ecr_repository" "worker" {

	name = "${var.project_name}/worker"

  #immutable so the tag cannot be overwritten
  #stops someone from overwriting the production image
  #good for security too

  image_tag_mutability = "IMMUTABLE"

  #free cve scan
  image_scanning_configuration {

    scan_on_push = true

  }

  #disregards any existing images
  force_delete = true

}

resource "aws_ecr_lifecycle_policy" "api" {

  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })

}

resource "aws_ecr_lifecycle_policy" "worker" {

  repository = aws_ecr_repository.worker.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })

}

