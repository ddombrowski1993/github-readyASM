alter table stores
add column if not exists service_type varchar(80) default 'Standard';

update stores
set service_type = 'Standard'
where service_type is null or trim(service_type) = '';
