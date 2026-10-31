from flask import Blueprint,render_template,current_app,jsonify
import sqlite3
from .health import check


def operations_blueprint(settings,jobs):
    bp=Blueprint('operations',__name__)
    @bp.get('/manage/status')
    def status():
        from paper_library.preferences import PreferenceError
        runtime=current_app.extensions['runtime_preferences']
        try:report=check(runtime.snapshot())
        except PreferenceError:
            report=check(settings)
            report['checks']['settings']={'status':'error'}
        runner=current_app.extensions.get('job_runner')
        if hasattr(runner,'status'):report['checks']['worker']=runner.status()
        index=current_app.extensions.get('search_index')
        if index is not None:
            report['checks']['search']={'status':'unavailable' if not index.enabled else 'warning' if index.errors else 'ok'}
        try:
            rows=jobs.recent(limit=20);events={row.id:jobs.events(row.id) for row in rows}
        except sqlite3.Error:rows=[];events={}
        return render_template('status.html',report=report,jobs=rows,events=events)
    @bp.get('/health/worker')
    def worker():
        report=current_app.extensions['job_runner'].status()
        return jsonify(report),200 if report['status']=='ok' else 503
    return bp
