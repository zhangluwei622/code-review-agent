def run(view, arguments, emit):
    for file in view.files():
        emit(file)
