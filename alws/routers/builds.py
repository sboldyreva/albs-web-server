import typing

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)

from alws import database
from alws.crud import build as build_crud, build_node
from alws.dependencies import get_db, JWTBearer
from alws.errors import DataNotFoundError
from alws.schemas import build_schema


router = APIRouter(
    prefix='/builds',
    tags=['builds'],
    dependencies=[Depends(JWTBearer())]
)


@router.post('/', response_model=build_schema.Build)
async def create_build(
            build: build_schema.BuildCreate,
            user: dict = Depends(JWTBearer()),
            db: database.Session = Depends(get_db)
        ):
    db_build = await build_crud.create_build(
        db, build, user['identity']['user_id'])
    return db_build


@router.get('/', response_model=typing.Union[
    typing.List[build_schema.Build], build_schema.BuildsResponse])
async def get_builds_per_page(
    request: Request,
    pageNumber: int,
    db: database.Session = Depends(get_db),
):
    search_params = build_schema.BuildSearch(**request.query_params)
    return await build_crud.get_builds(
        db=db,
        page_number=pageNumber,
        search_params=search_params,
    )


@router.get('/{build_id}/', response_model=build_schema.Build)
async def get_build(build_id: int, db: database.Session = Depends(get_db)):
    db_build = await build_crud.get_builds(db, build_id)
    if db_build is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Build with {build_id=} is not found'
        )
    return db_build


@router.patch('/{build_id}/restart-failed', response_model=build_schema.Build)
async def restart_failed_build_items(build_id: int,
                                     db: database.Session = Depends(get_db)):
    return await build_node.update_failed_build_items(db, build_id)


@router.delete('/{build_id}/remove', status_code=204)
async def remove_build(build_id: int, db: database.Session = Depends(get_db)):
    try:
        result = await build_crud.remove_build_job(db, build_id)
    except DataNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Build with {build_id=} is not found',
        )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Build with {build_id=} is released',
        )
    return result
