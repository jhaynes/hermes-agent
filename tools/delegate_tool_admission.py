"""Delegation execution admission, separate from prompt/schema limits."""
from gateway.load_admission import LoadAdmission

child_admission = LoadAdmission()


def deferred_result(**request):
    return {
        **request,
        'status': 'deferred', 'retryable': True,
        'note': 'System load limits new delegation. These tasks have NOT run and are NOT queued. '
                'Retry this request when load recovers or running children finish; do not bypass via inline execution.',
    }
