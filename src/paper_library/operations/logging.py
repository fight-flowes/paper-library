"""Bounded diagnostic logs without prompts, tokens or response bodies."""
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime,timezone
from paper_library.jobs.repository import _safe_error


class SafeFormatter(logging.Formatter):
    def format(self,record):
        value={'time':datetime.now(timezone.utc).isoformat(),'level':record.levelname,'logger':record.name,
               'message':_safe_error(record.getMessage())}
        if record.exc_info:value['exception_type']=record.exc_info[0].__name__
        return json.dumps(value,ensure_ascii=False)


def configure(settings):
    logger=logging.getLogger('paper_library');logger.setLevel(logging.INFO)
    path=settings.paths.state/'events.log'
    if any(getattr(h,'baseFilename',None)==str(path) for h in logger.handlers):return
    handler=RotatingFileHandler(path,maxBytes=int(settings.values.get('PAPER_LOG_MAX_MB','5'))*1024*1024,
                                backupCount=int(settings.values.get('PAPER_LOG_BACKUPS','3')),encoding='utf-8')
    handler.setFormatter(SafeFormatter());logger.addHandler(handler);logger.propagate=False
    for name in ("werkzeug","waitress"):
        access=logging.getLogger(name);access.handlers=[handler];access.propagate=False
