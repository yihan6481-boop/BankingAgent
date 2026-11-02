"""Recovery of existing nonfinancial writes using their original immutable receipt."""
def public_comparison(arguments):
    """Public catalog only. No owner/session scope and no personal SQL allowed."""
    from analysis_service import _compare,AnalysisError
    from analysis_models import ProductComparison
    from .pg02 import store,Cursor
    from .errors import StorageError
    from fastapi import HTTPException
    import psycopg
    try:
        with store()._pool.connection() as c,c.transaction():
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            class Catalog:
                def execute(self,query,parameters):
                    if query!='SELECT * FROM wealth_products WHERE product_id=?':
                        raise ValueError('public_catalog_sql_rejected')
                    return Cursor(c.execute('SELECT * FROM pg02.wealth_products WHERE product_id=%s',parameters))
            return ProductComparison.model_validate(_compare(Catalog(),arguments,None))
    except AnalysisError as error:
        raise HTTPException(422,error.code) from None
    except (psycopg.Error,StorageError):
        raise HTTPException(422,'analysis_data_unavailable') from None


def recover_mutation(context):
    from .pg02 import get_connection
    from utils import make_result
    import json
    db=None
    try:
        db=get_connection()
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('''SELECT m.result,m.execution_digest,r.result AS receipt_result
            FROM mutation_orders m JOIN lifecycle_receipts r ON r.operation_id=m.operation_id
            WHERE m.authorization_id=?''',(context.grant.authorization_id,)).fetchone()
        if row and row['execution_digest']==context.grant.action_digest and row['result']==row['receipt_result']:
            return json.loads(row['result'])
        code='commit_not_recorded'
        message='经原授权键核查未找到提交回执，变更未提交；请读取任务状态。'
    except Exception:
        code='commit_unknown'
        message='无法核实原请求提交状态，已停止自动执行；请使用原任务核查。'
    finally:
        if db: db.close()
    result=make_result(False,message)
    result['error_code']=code
    return result
