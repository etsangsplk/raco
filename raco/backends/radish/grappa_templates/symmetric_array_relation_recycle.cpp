// recycle result vectors {{sym}}
on_all_cores([=] {
    {{sym}}.data->clear();
});
