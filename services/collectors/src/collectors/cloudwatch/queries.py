TOP_ERRORS_QUERY = r"""
fields @timestamp, @message
| filter @message like /ERROR|Error|Exception|Traceback/
| stats count() as cnt by @message
| sort cnt desc
| limit 20
"""

RECENT_ERRORS_QUERY = r"""
fields @timestamp, @message, @logStream
| filter @message like /ERROR|Error|Exception|Traceback/
| sort @timestamp desc
| limit 50
"""
